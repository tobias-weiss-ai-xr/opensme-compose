//! Server-side RSS/Atom aggregation for the portal's news columns.
//!
//! The browser only ever talks to this origin (CSP `connect-src 'self'`), so
//! the portal itself fetches the external feeds and exposes a single
//! same-origin JSON endpoint (`/api/feeds`). Columns are configured via the
//! `PORTAL_FEEDS` env var (JSON); sensible German defaults cover
//! startup / market / legal news. Feeds are cached in-memory for `CACHE_TTL`
//! so a page refresh never re-hits the sources, and a failing upstream never
//! breaks the landing page (empty column instead of a 500).

use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::collections::HashMap;
use std::sync::RwLock;
use std::time::{Duration, Instant};

/// Hard limits — a runaway env var must not balloon memory or latency.
pub const MAX_COLUMNS: usize = 8;
pub const MAX_URLS_PER_COLUMN: usize = 6;
pub const MAX_ENTRIES_PER_COLUMN: usize = 12;
pub const MAX_ENTRIES_PER_FEED: usize = 10;
const MAX_FEED_BYTES: usize = 5 * 1024 * 1024;
const MAX_TITLE_CHARS: usize = 300;
const FETCH_TIMEOUT: Duration = Duration::from_secs(8);
const CACHE_TTL: Duration = Duration::from_secs(600);

/// One news column on the landing page (e.g. "Startup") with its feed URLs.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct FeedColumn {
    pub id: String,
    pub title: String,
    pub urls: Vec<String>,
}

/// A single headline, already normalized and safe to render as text.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct FeedEntry {
    pub title: String,
    pub url: String,
    /// ISO-8601 (RFC 3339) when the source provides a date; `None` otherwise.
    pub date: Option<String>,
    /// Human-readable feed name (the source host).
    pub source: String,
}

#[derive(Clone, Serialize)]
pub struct FeedColumnPayload {
    pub id: String,
    pub title: String,
    pub entries: Vec<FeedEntry>,
}

struct CachedColumn {
    entries: Vec<FeedEntry>,
    fetched_at: Instant,
}

/// In-memory feed cache + fetch pipeline. Shared behind `Arc<AppConfig>`.
pub struct FeedFetcher {
    columns: Vec<FeedColumn>,
    cache: RwLock<HashMap<String, CachedColumn>>,
    client: reqwest::Client,
}

/// Default columns: German startup / market / legal news. The whole set is
/// overridable via `PORTAL_FEEDS` (partial per-column overrides would force
/// an awkward merge; the section title always comes from the same place).
pub fn default_columns() -> Vec<FeedColumn> {
    vec![
        FeedColumn {
            id: "startup".into(),
            title: "Startup".into(),
            urls: vec![
                "https://t3n.de/rss.xml".into(),
                "https://www.deutsche-startups.de/feed/".into(),
            ],
        },
        FeedColumn {
            id: "markt".into(),
            title: "Markt".into(),
            urls: vec![
                "https://www.manager-magazin.de/index.rss".into(),
                "https://www.handelsblatt.com/contentexport/feed/finanzen".into(),
            ],
        },
        FeedColumn {
            id: "legal".into(),
            title: "Legal".into(),
            urls: vec!["https://www.lto.de/rss/feed.xml".into()],
        },
    ]
}

/// Parse `PORTAL_FEEDS` (JSON: `[{"id","title","urls":[…]}]`). Unset / blank /
/// invalid JSON → defaults. Ids are lowercased, only http(s) urls survive,
/// and columns beyond the hard limits are dropped.
pub fn parse_columns(raw: &str) -> Vec<FeedColumn> {
    if raw.trim().is_empty() {
        return default_columns();
    }
    match serde_json::from_str::<Vec<FeedColumn>>(raw) {
        Ok(mut cols) => {
            cols.truncate(MAX_COLUMNS);
            for col in &mut cols {
                col.id = col.id.trim().to_ascii_lowercase();
                col.title = col.title.trim().to_string();
                col.urls
                    .retain(|u| u.starts_with("https://") || u.starts_with("http://"));
                col.urls.truncate(MAX_URLS_PER_COLUMN);
            }
            // Drop columns that would render as an empty or dead card.
            cols.retain(|c| !c.id.is_empty() && !c.title.is_empty() && !c.urls.is_empty());
            cols
        }
        Err(_) => default_columns(),
    }
}

impl FeedFetcher {
    pub fn new(columns: Vec<FeedColumn>) -> Self {
        let mut builder = reqwest::Client::builder()
            .timeout(FETCH_TIMEOUT)
            .user_agent(
                "openSMEPortal/1.0 (+https://github.com/tobias-weiss-ai-xr/opensme-compose)",
            );
        if let Some(proxy) = proxy_from_env() {
            builder = builder.proxy(proxy);
        }
        let client = builder.build().unwrap_or_default();
        FeedFetcher {
            columns,
            cache: RwLock::new(HashMap::new()),
            client,
        }
    }

    /// Configured columns in order — used to render the feed cards.
    pub fn columns(&self) -> &[FeedColumn] {
        &self.columns
    }

    /// Current snapshot of every column: cached-fresh entries if available,
    /// otherwise a (concurrent) re-fetch. Never fails — a broken upstream
    /// only yields an empty column.
    pub async fn snapshot(&self) -> Vec<FeedColumnPayload> {
        let cols = self.columns.clone();
        let now = Instant::now();

        let futures: Vec<_> = cols
            .into_iter()
            .map(|col| {
                let cached = {
                    let cache = self.cache.read().unwrap_or_else(|e| e.into_inner());
                    cache.get(&col.id).and_then(|c| {
                        if now.duration_since(c.fetched_at) < CACHE_TTL {
                            Some(c.entries.clone())
                        } else {
                            None
                        }
                    })
                };
                async move {
                    match cached {
                        Some(entries) => (col, entries),
                        None => {
                            let entries = self.fetch_column(&col).await;
                            (col, entries)
                        }
                    }
                }
            })
            .collect();

        futures::future::join_all(futures)
            .await
            .into_iter()
            .map(|(col, entries)| FeedColumnPayload {
                id: col.id,
                title: col.title,
                entries,
            })
            .collect()
    }

    /// Fetch every URL of a column concurrently, merge + sort + cap, and
    /// cache the result (only when something was actually retrieved).
    async fn fetch_column(&self, col: &FeedColumn) -> Vec<FeedEntry> {
        let urls: Vec<&str> = col
            .urls
            .iter()
            .take(MAX_URLS_PER_COLUMN)
            .map(String::as_str)
            .collect();
        let per_url: Vec<Vec<FeedEntry>> =
            futures::future::join_all(urls.into_iter().map(|u| self.fetch_url(u))).await;
        let mut entries: Vec<FeedEntry> = per_url.into_iter().flatten().collect();
        entries = merge_and_cap(entries);
        if !entries.is_empty() {
            self.cache.write().unwrap_or_else(|e| e.into_inner()).insert(
                col.id.clone(),
                CachedColumn {
                    entries: entries.clone(),
                    fetched_at: Instant::now(),
                },
            );
        }
        entries
    }

    async fn fetch_url(&self, url: &str) -> Vec<FeedEntry> {
        let source = source_host(url);
        let resp = match self.client.get(url).send().await {
            Ok(r) if r.status().is_success() => r,
            _ => return Vec::new(),
        };
        let bytes = match resp.bytes().await {
            Ok(b) if !b.is_empty() && b.len() <= MAX_FEED_BYTES => b,
            _ => return Vec::new(),
        };
        parse_feed(&bytes, &source)
    }
}

/// Egress-restricted hosts (e.g. OpenVZ containers without working
/// MASQUERADE on the virtualization layer) can still run a local HTTP
/// CONNECT proxy reachable from the docker bridges. reqwest only honors
/// `HTTPS_PROXY` with its opt-in `system-proxy` feature, which this build
/// deliberately does not use — so read the conventional variables here and
/// wire the proxy into the feed client only (internal clients unaffected).
fn proxy_env_url() -> Option<String> {
    let raw = std::env::var("HTTPS_PROXY")
        .or_else(|_| std::env::var("https_proxy"))
        .ok()?;
    normalize_proxy_candidate(&raw)
}

pub(crate) fn proxy_from_env() -> Option<reqwest::Proxy> {
    proxy_env_url().and_then(|u| reqwest::Proxy::https(&u).ok())
}

/// Accept a non-empty candidate, trimmed of surrounding whitespace and a
/// single trailing slash.
fn normalize_proxy_candidate(raw: &str) -> Option<String> {
    let trimmed = raw.trim().trim_end_matches('/');
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// Human-readable source label: host w/o scheme and `www.` (`t3n.de`).
fn source_host(url: &str) -> String {
    let rest = url
        .strip_prefix("https://")
        .or_else(|| url.strip_prefix("http://"))
        .unwrap_or(url);
    let host = rest.split(['/', '?', '#']).next().unwrap_or("").to_string();
    host.strip_prefix("www.").unwrap_or(&host).to_string()
}

/// Parse RSS/Atom bytes with feed-rs; map to normalized entries. Malformed
/// input or a feed without usable entries yields an empty vec (never an
/// error) — the client renders "unavailable" instead of breaking the page.
pub fn parse_feed(bytes: &[u8], source: &str) -> Vec<FeedEntry> {
    let Ok(feed) = feed_rs::parser::parse(bytes) else {
        return Vec::new();
    };
    feed.entries
        .iter()
        .take(MAX_ENTRIES_PER_FEED)
        .filter_map(|e| {
            let title = e
                .title
                .as_ref()
                .map(|t| t.content.trim().to_string())
                .filter(|t| !t.is_empty())?;
            let href = e
                .links
                .iter()
                .find(|l| l.rel.as_deref() == Some("alternate"))
                .or_else(|| e.links.first())
                .map(|l| l.href.trim().to_string())
                .filter(|h| h.starts_with("https://") || h.starts_with("http://"))?;
            let date = e.published.or(e.updated).map(|d| d.to_rfc3339());
            Some(FeedEntry {
                title: truncate_title(&title),
                url: href,
                date,
                source: source.to_string(),
            })
        })
        .collect()
}

fn truncate_title(s: &str) -> String {
    let mut chars = s.chars();
    let mut out: String = chars.by_ref().take(MAX_TITLE_CHARS).collect();
    if chars.next().is_some() {
        out.push('…');
    }
    out
}

/// Sort newest-first (dated entries before undated ones) and cap the column.
/// ISO-8601 strings from `DateTime<Utc>` compare lexicographically in order.
pub fn merge_and_cap(mut entries: Vec<FeedEntry>) -> Vec<FeedEntry> {
    entries.sort_by(|a, b| match (&a.date, &b.date) {
        (Some(x), Some(y)) => y.cmp(x),
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (None, None) => Ordering::Equal,
    });
    entries.truncate(MAX_ENTRIES_PER_COLUMN);
    entries
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(title: &str, date: Option<&str>) -> FeedEntry {
        FeedEntry {
            title: title.into(),
            url: "https://example.com/x".into(),
            date: date.map(str::to_string),
            source: "example.com".into(),
        }
    }

    #[test]
    fn default_columns_when_unset_or_invalid() {
        for raw in ["", "   ", "not json", r#"{"id":"x"}"#] {
            let cols = parse_columns(raw);
            assert_eq!(cols.len(), 3, "raw={raw:?}");
            assert_eq!(cols[0].id, "startup");
            assert!(cols.iter().all(|c| !c.urls.is_empty()));
        }
    }

    #[test]
    fn parse_columns_normalizes_and_drops_flawed() {
        let raw = r#"[
            {"id":"Foo","title":"Foo News","urls":["https://a.example/feed","ftp://bad","http://b.example/rss"]},
            {"id":"Bar","title":"","urls":["https://c.example/"]}
        ]"#;
        let cols = parse_columns(raw);
        assert_eq!(cols.len(), 1, "empty-title column dropped");
        assert_eq!(cols[0].id, "foo"); // lowercased
        assert_eq!(cols[0].title, "Foo News");
        assert_eq!(cols[0].urls.len(), 2, "non-http url dropped");
    }

    #[test]
    fn parse_columns_caps_columns_and_urls() {
        let mut items = Vec::new();
        for i in 0..(MAX_COLUMNS + 3) {
            let mut urls = vec!["https://a.example/feed".to_string()];
            for j in 0..(MAX_URLS_PER_COLUMN + 2) {
                urls.push(format!("https://{j}.example/feed"));
            }
            items.push(serde_json::json!({
                "id": format!("c{i}"),
                "title": format!("Column {i}"),
                "urls": urls,
            }));
        }
        let cols = parse_columns(&serde_json::to_string(&items).unwrap());
        assert!(cols.len() <= MAX_COLUMNS);
        assert!(cols.iter().all(|c| c.urls.len() <= MAX_URLS_PER_COLUMN));
    }

    #[test]
    fn source_host_strips_scheme_and_www() {
        assert_eq!(source_host("https://t3n.de/rss.xml"), "t3n.de");
        assert_eq!(
            source_host("https://www.deutsche-startups.de/feed/"),
            "deutsche-startups.de"
        );
        assert_eq!(
            source_host("http://manager-magazin.de/x"),
            "manager-magazin.de"
        );
    }

    #[test]
    fn parse_feed_extracts_rss_entries() {
        let rss = br#"<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <title>Beispiel</title>
          <item>
            <title>Erste Meldung</title>
            <link>https://example.com/eins</link>
            <pubDate>Wed, 01 Jan 2025 10:00:00 +0000</pubDate>
          </item>
          <item>
            <title>Zweite &amp; Meldung</title>
            <link>https://example.com/zwei</link>
          </item>
          <item><title>ohne Link</title></item>
        </channel></rss>"#;
        let entries = parse_feed(rss, "example.com");
        assert_eq!(entries.len(), 2, "entry without link skipped");
        assert_eq!(entries[0].title, "Erste Meldung");
        assert_eq!(entries[0].url, "https://example.com/eins");
        assert!(entries[0].date.is_some());
        assert_eq!(entries[0].source, "example.com");
        assert_eq!(entries[1].date, None);
        assert_eq!(entries[1].title, "Zweite & Meldung");
    }

    #[test]
    fn parse_feed_handles_atom_and_garbage() {
        let atom = br#"<feed xmlns="http://www.w3.org/2005/Atom">
          <title>Atom</title>
          <entry>
            <title>Atom Post</title>
            <link rel="alternate" href="https://example.com/a"/>
            <published>2025-02-01T08:00:00Z</published>
          </entry>
        </feed>"#;
        let entries = parse_feed(atom, "example.com");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].title, "Atom Post");
        assert!(entries[0].date.is_some());

        assert!(parse_feed(b"<not xml at all", "example.com").is_empty());
        assert!(parse_feed(b"", "example.com").is_empty());
    }

    #[test]
    fn normalize_proxy_candidate_trims_and_rejects_empty() {
        assert_eq!(
            normalize_proxy_candidate(" http://gateway.internal:8888/ "),
            Some("http://gateway.internal:8888".to_string())
        );
        assert_eq!(normalize_proxy_candidate(""), None);
        assert_eq!(normalize_proxy_candidate("   "), None);
        assert_eq!(
            normalize_proxy_candidate("http://p:1//"),
            Some("http://p:1".to_string())
        );
    }

    #[test]
    fn truncate_title_caps_at_max_chars() {
        let long = "x".repeat(MAX_TITLE_CHARS + 20);
        let t = truncate_title(&long);
        assert!(t.chars().count() <= MAX_TITLE_CHARS + 1, "ellipsis allowed");
        assert!(t.ends_with('…'));
        assert_eq!(truncate_title("short"), "short");
    }

    #[test]
    fn merge_sorts_newest_first_undated_last_and_caps() {
        let ordered = vec![
            entry("alt", Some("2025-01-01T00:00:00+00:00")),
            entry("neu", Some("2025-06-01T00:00:00+00:00")),
            entry("undatiert", None),
            entry("mittel", Some("2025-03-01T00:00:00+00:00")),
        ];
        let got = merge_and_cap(ordered.clone());
        let titles: Vec<&str> = got.iter().map(|e| e.title.as_str()).collect();
        assert_eq!(titles, vec!["neu", "mittel", "alt", "undatiert"]);

        // Cap: 17 dated entries → 12, newest first (fixedOffset makes them
        // clearly ordered).
        let mut many: Vec<FeedEntry> = (0..17)
            .map(|i| FeedEntry {
                title: format!("n{i:02}"),
                url: "https://e/x".into(),
                date: Some(format!("2025-01-01T00:{i:02}:00+00:00")),
                source: "e".into(),
            })
            .collect();
        many.reverse();
        let capped = merge_and_cap(many);
        assert_eq!(capped.len(), MAX_ENTRIES_PER_COLUMN);
        assert_eq!(capped[0].title, "n16");
    }
}
