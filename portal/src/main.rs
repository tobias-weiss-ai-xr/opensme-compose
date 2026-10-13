use axum::{
    extract::State,
    http::StatusCode,
    middleware,
    response::{Html, IntoResponse, Json},
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::signal;
use tower_http::cors::{Any, CorsLayer};
use tracing::{info, warn};

mod feeds;
mod intercom;
mod logout;
mod metrics;
mod polls;

#[derive(Clone)]
struct AppConfig {
    opencloud_url: String,
    mail_url: String,
    idp_url: String,
    ticketing_url: String,
    cms_url: String,
    shop_url: String,
    bpm_url: String,
    invoice_url: String,
    paperless_url: String,
    chat_url: String,
    notes_url: String,
    surveys_url: String,
    portal_domain: String,
    opensme_domain: String,
    announcements: Arc<Vec<Announcement>>,
    ai: Option<AiConfig>,
    ai_client: reqwest::Client,
    intercom: intercom::IntercomConfig,
    intercom_store: Arc<intercom::IntercomStore>,
    intercom_client: reqwest::Client,
    polls: Option<Arc<polls::PollStore>>,
    feed_fetcher: Arc<feeds::FeedFetcher>,
}

#[derive(Clone, Debug, PartialEq)]
struct AiConfig {
    api_url: String,
    model: String,
    api_key: Option<String>,
}

/// Portal "intercom": operator-broadcast banners (info / warn).
#[derive(Clone, Debug, PartialEq, Serialize)]
struct Announcement {
    level: String,
    text: String,
}

#[derive(Serialize)]
struct Service {
    id: String,
    name: String,
    description: String,
    url: String,
}

/// Cap the banner count so a runaway env var can't balloon the page.
const MAX_ANNOUNCEMENTS: usize = 8;

/// HTML-escape a string to prevent XSS in rendered templates.
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

fn load_env(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| {
        warn!(%key, "environment variable not set, using default");
        default.to_string()
    })
}

/// Parse PORTAL_ANNOUNCEMENTS: a JSON array of {"level": "info"|"warn",
/// "text": "…"}. Unset / empty → no banners. Invalid input is logged and
/// ignored (never break the landing page over a cosmetic feature).
fn parse_announcements(raw: &str) -> Vec<Announcement> {
    if raw.trim().is_empty() {
        return Vec::new();
    }
    #[derive(Deserialize)]
    struct RawAnnouncement {
        level: Option<String>,
        text: String,
    }
    match serde_json::from_str::<Vec<RawAnnouncement>>(raw) {
        Ok(items) => items
            .into_iter()
            .filter(|a| !a.text.trim().is_empty())
            .map(|a| Announcement {
                level: if a.level.as_deref() == Some("warn") {
                    "warn".to_string()
                } else {
                    "info".to_string()
                },
                text: a.text,
            })
            .take(MAX_ANNOUNCEMENTS)
            .collect(),
        Err(e) => {
            warn!(%e, "invalid PORTAL_ANNOUNCEMENTS json — ignoring");
            Vec::new()
        }
    }
}

/// Server-render the intercom list into the landing page — progressive
/// enhancement: the page is meaningful without JS; app.js re-renders
/// after each post (textContent, never innerHTML).
fn render_intercom(messages: &[intercom::IntercomMessage]) -> String {
    if messages.is_empty() {
        return r#"<li class=\"ic-empty\">No notes yet — start the conversation.</li>"#.into();
    }
    messages
        .iter()
        .map(|m| {
            let text = html_escape(&m.text);
            let att = match &m.attachment {
                Some(a) => {
                    let url = html_escape(&a.url);
                    let name = html_escape(&a.name);
                    let size = match a.size {
                        Some(b) if b < 1024 => format!("{b} B"),
                        Some(b) if b < 1024 * 1024 => {
                            format!("{:.1} KB", b as f64 / 1024.0)
                        }
                        Some(b) => format!("{:.1} MB", b as f64 / (1024.0 * 1024.0)),
                        None => String::new(),
                    };
                    let size_attr = if size.is_empty() {
                        String::new()
                    } else {
                        format!(" ({size})")
                    };
                    let ct = html_escape(&a.content_type);
                    format!(
                        r#"<a class=\"ic-att\" href=\"{url}\" target=\"_blank\" rel=\"noopener noreferrer\">📎 {name}{size_attr} · {ct}</a>"#
                    )
                }
                None => String::new(),
            };
            format!(r#"<li>{text}{att}</li>"#)
        })
        .collect()
}

fn render_announcements(announcements: &[Announcement]) -> String {
    announcements
        .iter()
        .map(|a| {
            let cls = if a.level == "warn" {
                "announcement warn"
            } else {
                "announcement info"
            };
            let text = html_escape(&a.text);
            format!(r#"<div class="{cls}" role="status"><span class="pill">⚑</span> {text}</div>"#)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Emoji glyph for a service/func card (decorative, never user-controlled).
fn service_icon(id: &str) -> &'static str {
    match id {
        "opencloud" => "☁",
        "webmail" => "✉",
        "support" => "🆘",
        "website" => "🌐",
        "shop" => "🛒",
        "workflow" => "⚙",
        "invoice" => "🧾",
        "archive" => "🗄",
        "chat" => "🗨",
        "notes" => "📋",
        "surveys" => "🗳",
        _ => "🧩",
    }
}

/// One news column card per configured feed column; the client fills the
/// list from /api/feeds (CSP keeps connect-src 'self').
fn render_feed_cards(columns: &[feeds::FeedColumn]) -> String {
    columns
        .iter()
        .map(|c| {
            let cid = html_escape(&c.id);
            let title = html_escape(&c.title);
            format!(
                r#"<div class="feed-card"><h2>{title}</h2><ul id="feed-{cid}" aria-live="polite"><li class="loading">Lade Meldungen…</li></ul></div>"#
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn build_landing_page(config: &AppConfig) -> String {
    let services = get_services(config);
    let cards: String = services
        .iter()
        .map(|s| {
            let icon = service_icon(&s.id);
            let name = html_escape(&s.name);
            let desc = html_escape(&s.description);
            let url = html_escape(&s.url);
            format!(
                r#"<a href="{url}" class="card func-card" data-pal="{name}" target="_blank" rel="noopener noreferrer">
                <span class="func-icon" aria-hidden="true">{icon}</span>
                <h3>{name}</h3>
                <p class="product">{desc}</p>
                <span class="open">Öffnen</span>
            </a>"#
            )
        })
        .collect::<Vec<_>>()
        .join("\n");

    let ai_card = if config.ai.is_some() {
        r#"<div class="card func-card ai" id="ai-card">
                <span class="func-icon" aria-hidden="true">🧠</span>
                <h3>KI-Assistent</h3>
                <p class="product">Fragen an deine Wissensbasis</p>
                <div class="ai-box">
                    <input class="ai-q" type="text" placeholder="Frage stellen…" aria-label="Frage">
                    <button class="ai-ask" type="button">Stellen</button>
                    <p class="ai-out" hidden></p>
                </div>
            </div>"#
    } else {
        ""
    };

    let ic_rendered = render_intercom(&config.intercom_store.list());
    let polls_card = match &config.polls {
        Some(_) => r#"<div class="card func-card polls" id="polls-card">
                <span class="func-icon" aria-hidden="true">📊</span>
                <h3>Umfragen</h3>
                <p class="product">Team-Abstimmungen — optional mit Dokument aus der Cloud</p>
                <ul class="pl-list" id="pl-list" aria-live="polite"><li class="ic-empty">Lade Umfragen…</li></ul>
                <details class="pl-new">
                    <summary>Neue Umfrage anlegen</summary>
                    <div class="pl-box">
                        <input class="pl-question" maxlength="300" placeholder="Frage…" aria-label="Frage">
                        <textarea class="pl-options" rows="3" placeholder="Antwortoptionen — eine pro Zeile (min. 2)" aria-label="Antwortoptionen"></textarea>
                        <input class="ic-url pl-url" type="url" placeholder="Anhang aus der Cloud — geteilten Link einfügen (https://cloud…/s/…)" aria-label="Umfrage-Anhang-URL">
                        <div class="ic-actions">
                            <label class="ic-file-label" for="pl-file">📎 Datei…</label>
                            <input class="ic-file" id="pl-file" type="file" aria-label="Umfrage-Anhang" hidden>
                            <span class="ic-file-name" aria-live="polite"></span>
                            <button class="ic-send pl-create" type="button">Anlegen</button>
                        </div>
                        <p class="ic-out pl-out" hidden></p>
                    </div>
                </details>
            </div>"#
        .to_string(),
        None => String::new(),
    };
    let intercom_card = format!(
        r#"<div class="card func-card intercom" id="intercom-card">
                <span class="func-icon" aria-hidden="true">💬</span>
                <h3>Team-Notizen</h3>
                <p class="product">Kurze Notiz für das Team</p>
                <ul class="ic-list" id="ic-list" aria-live="polite">{ic_rendered}</ul>
                <div class="ic-box">
                    <textarea class="ic-text" rows="2" placeholder="Kurze Notiz für das Team…" aria-label="Nachricht" maxlength="2000"></textarea>
                    <input class="ic-url" type="url" placeholder="Anhang aus der Cloud — geteilten Link einfügen (https://cloud…/s/…)" aria-label="Anhang-URL">
                    <div class="ic-actions">
                        <label class="ic-file-label" for="ic-file">📎 Datei…</label>
                        <input class="ic-file" id="ic-file" type="file" aria-label="Datei anhängen" hidden>
                        <span class="ic-file-name" aria-live="polite"></span>
                        <button class="ic-send" type="button">Senden</button>
                    </div>
                    <p class="ic-out" hidden></p>
                </div>
            </div>"#
    );

    let announcements = render_announcements(&config.announcements);
    let feed_cards = render_feed_cards(config.feed_fetcher.columns());
    let domain = html_escape(&config.opensme_domain);

    format!(
        r##"<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>openSME-Portal</title>
    <meta name="description" content="openSME — deine selbst gehostete Suite: Cloud, Mail, Zusammenarbeit, Identität und Workflow-Automatisierung.">
    <meta name="theme-color" content="#005CB9">
    <meta property="og:title" content="openSME-Portal">
    <meta property="og:description" content="Dein Startpunkt für alle Dienste.">
    <meta property="og:type" content="website">
    <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23005cb9'/%3E%3Ctext x='16' y='22' font-family='system-ui,sans-serif' font-size='18' font-weight='700' fill='white' text-anchor='middle'%3EoS%3C/text%3E%3C/svg%3E">
    <meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; script-src 'self'; connect-src 'self';">
    <script src="/app.js" defer></script>
    <style>
        :root {{
            --bg: #F3F5F7;
            --card: #FFFFFF;
            --border: #E2E6EA;
            --text: #22272E;
            --text-dim: #5F6B7A;
            --accent: #005CB9;
            --accent-light: #E8F1FB;
            --accent-hover: #E5732D;
            --footer-bg: #003E7C;
            --footer-text: #FFFFFF;
            --shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
            --radius: 10px;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: "Helvetica Neue", Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }}
        .container {{ width: 100%; max-width: 1200px; margin: 0 auto; padding: 20px; }}
        main.container {{ flex: 1; }}

        /* Announcements (operator broadcast banners) */
        .announcement {{
            text-align: center;
            padding: 0.6rem 1rem;
            font-size: 0.92rem;
        }}
        .announcement .pill {{ opacity: 0.8; }}
        .announcement.info {{ background: var(--accent-light); color: var(--accent); border-bottom: 1px solid var(--border); }}
        .announcement.warn {{ background: #FFF4E5; color: #B25E00; border-bottom: 1px solid var(--border); }}

        /* Header */
        header {{ background: var(--accent); color: #fff; box-shadow: 0 2px 6px rgba(0,0,0,0.15); }}
        .header-row {{
            display: flex; align-items: center; justify-content: space-between;
            flex-wrap: wrap; gap: 16px; padding: 18px 20px;
        }}
        .brand h1 {{ font-size: 26px; margin-bottom: 2px; letter-spacing: 0.3px; }}
        .brand p {{ opacity: 0.9; font-size: 15px; }}
        .user-nav {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
        .btn-ghost {{
            text-decoration: none; font-size: 14px; font-weight: 600;
            border-radius: 999px; padding: 7px 16px; color: #fff;
            border: 1px solid rgba(255,255,255,0.6);
            transition: background 0.2s, color 0.2s;
        }}
        .btn-ghost:hover {{ background: rgba(255,255,255,0.15); }}

        /* Sections */
        .section-title {{ color: var(--accent); font-size: 20px; margin: 26px 0 14px; }}

        /* Service + widget tiles */
        .func-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }}
        .func-card {{
            background: var(--card); border: 1px solid var(--border);
            border-radius: var(--radius); box-shadow: var(--shadow);
            padding: 22px 18px; text-align: center;
            transition: transform 0.15s ease, box-shadow 0.15s ease;
            display: flex; flex-direction: column; gap: 2px;
        }}
        a.func-card {{ text-decoration: none; color: inherit; }}
        .func-card:hover {{ transform: translateY(-3px); box-shadow: 0 6px 14px rgba(0,0,0,0.12); }}
        .func-icon {{
            width: 50px; height: 50px; margin: 0 auto 10px;
            display: grid; place-items: center; font-size: 24px;
            background: var(--accent-light); color: var(--accent); border-radius: 14px;
        }}
        .func-card h3 {{ color: var(--accent); margin-bottom: 2px; font-size: 15px; }}
        .func-card .product {{ color: var(--text-dim); font-size: 13px; font-style: italic; }}
        .func-card .open {{
            display: block; margin-top: 12px;
            background: var(--accent); color: #fff;
            padding: 9px 14px; border-radius: 999px;
            text-decoration: none; font-weight: 600; font-size: 14px;
        }}
        a.func-card:hover .open {{ background: var(--accent-hover); }}

        /* Feed columns */
        .feeds {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin: 22px 0 4px; }}
        .feed-card {{
            background: var(--card); border: 1px solid var(--border);
            border-radius: var(--radius); box-shadow: var(--shadow); padding: 16px 18px;
        }}
        .feed-card h2 {{ color: var(--accent); font-size: 16px; margin-bottom: 8px; border-bottom: 2px solid var(--accent-light); padding-bottom: 6px; }}
        .feeds ul {{ list-style: none; padding: 0; }}
        .feeds li {{ padding: 7px 0; border-bottom: 1px solid var(--border); font-size: 13.5px; }}
        .feeds li:last-child {{ border-bottom: none; }}
        .feeds li a {{ color: var(--accent); text-decoration: none; }}
        .feeds li a:hover {{ text-decoration: underline; }}
        .feeds li .date {{ color: var(--text-dim); font-size: 11.5px; display: block; margin-top: 2px; }}
        .loading, .error {{ color: var(--text-dim); font-style: italic; }}
        .error {{ color: #d32f2f; }}

        /* AI box (inside its func-card) */
        .ai-box {{ display: flex; flex-direction: column; gap: 0.6rem; margin-top: 12px; text-align: left; }}
        .ai-q {{
            background: #fff; border: 1px solid var(--border); border-radius: 6px;
            color: var(--text); padding: 8px 10px; font-size: 14px; width: 100%;
        }}
        .ai-ask {{
            align-self: flex-start; background: var(--accent); color: #fff;
            border: none; border-radius: 999px; padding: 7px 16px;
            font-size: 13px; font-weight: 600; cursor: pointer;
        }}
        .ai-ask:hover {{ background: var(--accent-hover); }}
        .ai-out {{ color: var(--text); font-size: 0.92rem; line-height: 1.55; white-space: pre-wrap; }}

        /* Intercom (team notes) */
        .ic-list {{
            list-style: none; display: flex; flex-direction: column; gap: 0.5rem;
            max-height: 16rem; overflow-y: auto; margin-top: 12px; text-align: left;
        }}
        .ic-list li {{ color: var(--text); font-size: 0.92rem; line-height: 1.5; word-break: break-word; border-bottom: 1px solid var(--border); padding: 4px 0; }}
        .ic-list li:last-child {{ border-bottom: none; }}
        .ic-list .ic-att {{ display: block; color: var(--accent); font-size: 0.85rem; text-decoration: none; }}
        .ic-list .ic-att:hover {{ text-decoration: underline; }}
        .ic-box {{ display: flex; flex-direction: column; gap: 0.6rem; margin-top: 0.75rem; text-align: left; }}
        .ic-text, .ic-url {{
            border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
            font-size: 14px; width: 100%; color: var(--text); background: #fff;
            font-family: inherit; resize: vertical;
        }}
        .ic-send {{
            align-self: flex-start; background: var(--accent); color: #fff;
            border: none; border-radius: 999px; padding: 7px 16px;
            font-size: 13px; font-weight: 600; cursor: pointer;
        }}
        .ic-send:hover {{ background: var(--accent-hover); }}
        .ic-actions {{ display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }}
        .ic-file-label {{
            background: var(--accent-light); color: var(--accent); border-radius: 999px;
            padding: 6px 14px; font-size: 13px; font-weight: 600; cursor: pointer;
        }}
        .ic-file-label:hover {{ background: #d7e7f8; }}
        .ic-file-name {{ color: var(--text-dim); font-size: 0.85rem; }}
        .ic-out {{ color: var(--text-dim); font-size: 0.88rem; }}

        /* Polls (Umfragen) — optional card */
        .pl-list {{ list-style: none; padding: 0; margin: 0.5rem 0; text-align: left; display: flex; flex-direction: column; gap: 0.9rem; }}
        .poll-q {{ font-weight: 600; font-size: 0.95rem; margin-bottom: 0.4rem; }}
        .poll-att {{ display: inline-block; font-size: 0.85rem; margin-bottom: 0.4rem; }}
        .poll-opts {{ display: flex; flex-direction: column; gap: 0.35rem; }}
        .poll-opt {{
            position: relative; overflow: hidden; display: flex; align-items: center;
            justify-content: space-between; gap: 0.5rem;
            border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px;
            font-size: 13px; background: #fff;
        }}
        .poll-fill {{ position: absolute; inset: 0; background: var(--accent-light); width: 0; }}
        .poll-opt > * {{ position: relative; }}
        .poll-label {{ flex: 1; text-align: left; word-break: break-word; }}
        .poll-count {{ color: var(--text-dim); font-variant-numeric: tabular-nums; white-space: nowrap; }}
        .poll-vote {{
            border: none; background: none; font: inherit; font-weight: 600;
            color: var(--accent); cursor: pointer; white-space: nowrap; padding: 0;
        }}
        .poll-vote:hover {{ text-decoration: underline; }}
        .pl-new {{ margin-top: 0.6rem; text-align: left; }}
        .pl-new summary {{ cursor: pointer; color: var(--accent); font-size: 0.9rem; font-weight: 600; }}
        .pl-box {{ display: flex; flex-direction: column; gap: 0.5rem; margin-top: 0.6rem; }}
        .pl-question, .pl-options {{
            border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
            font-size: 14px; width: 100%; background: #fff; font-family: inherit;
            box-sizing: border-box; resize: vertical;
        }}

        /* Quick-switch palette (Ctrl+K) */
        #palette {{
            position: fixed; inset: 0; background: rgba(0, 30, 60, 0.45);
            display: flex; align-items: flex-start; justify-content: center;
            padding-top: 14vh; z-index: 40;
        }}
        #palette[hidden] {{ display: none; }}
        .pal-box {{
            width: min(520px, 92vw); background: #fff;
            border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
            box-shadow: 0 24px 64px rgba(0, 0, 0, 0.25);
        }}
        #pal-input {{
            width: 100%; background: transparent; border: none;
            border-bottom: 1px solid var(--border); color: var(--text);
            font-size: 1.05rem; padding: 0.9rem 1.1rem; outline: none;
        }}
        #pal-list {{ list-style: none; max-height: 46vh; overflow-y: auto; }}
        #pal-list li {{ padding: 0.6rem 1.1rem; color: var(--text); cursor: pointer; font-size: 0.98rem; }}
        #pal-list li.sel {{ background: var(--accent-light); color: var(--accent); }}
        #pal-list li.none {{ color: var(--text-dim); cursor: default; }}
        .pal-hint {{ padding: 0.5rem 1.1rem 0.7rem; color: var(--text-dim); font-size: 0.78rem; border-top: 1px solid var(--border); }}
        #pal-hint {{
            position: fixed; right: 1.1rem; bottom: 1.1rem;
            background: var(--accent); color: #fff; border: none;
            border-radius: 999px; font-size: 0.8rem; font-weight: 600;
            padding: 0.4rem 0.7rem; cursor: pointer; z-index: 30;
        }}
        #pal-hint:hover {{ background: var(--accent-hover); }}

        footer {{
            background: var(--footer-bg); color: var(--footer-text);
            padding: 22px 0; text-align: center; font-size: 14px; margin-top: 28px;
        }}
        footer a {{ color: var(--footer-text); text-decoration: none; opacity: 0.85; }}
        footer a:hover {{ opacity: 1; text-decoration: underline; }}
        footer .row {{ margin-top: 8px; }}

        @media (max-width: 640px) {{
            .header-row {{ flex-direction: column; align-items: flex-start; }}
            .brand h1 {{ font-size: 22px; }}
        }}
    </style>
</head>
<body>
    {announcements}
    <header>
        <div class="container header-row">
            <div class="brand">
                <h1>openSME</h1>
                <p>Dein Startpunkt für alle Dienste</p>
            </div>
            <nav class="user-nav" aria-label="Navigation">
                <a class="btn-ghost" href="#dienste">Dienste</a>
                <a class="btn-ghost" href="#aktuelles">Aktuelles</a>
            </nav>
        </div>
    </header>
    <main class="container">
        <section aria-label="Dienste" id="dienste">
            <h2 class="section-title">Dienste</h2>
            <div class="func-grid">
                {cards}
                {ai_card}
                {intercom_card}
                {polls_card}
            </div>
        </section>
        <section aria-label="Aktuelles" id="aktuelles">
            <h2 class="section-title">Aktuelles</h2>
            <div class="feeds">
                {feed_cards}
            </div>
        </section>
    </main>
    <footer>
        <div class="container">
            <p>openSME — die selbst gehostete Suite für kleine und mittlere Unternehmen.</p>
            <p class="row">Support &amp; Consulting: <a href="mailto:hello@opensme.org">hello@opensme.org</a> &middot; {domain}</p>
        </div>
    </footer>
</body>
</html>"##
    )
}

/// Return only services that have a non-default URL (i.e. actually configured).
fn get_services(config: &AppConfig) -> Vec<Service> {
    let mut services = Vec::new();

    // Catalog functions only — infrastructure (identity/auth, embedded
    // editors, mail backends) never gets a tile; see openspec capability
    // portal-catalog.

    // OpenCloud — only if URL differs from the raw default
    if !config.opencloud_url.is_empty() {
        services.push(Service {
            id: "opencloud".into(),
            name: "OpenCloud".into(),
            description: "Cloud storage, file sharing and collaboration".into(),
            url: config.opencloud_url.clone(),
        });
    }

    // Webmail — only if explicitly configured (not empty)
    if !config.mail_url.is_empty() {
        services.push(Service {
            id: "webmail".into(),
            name: "Webmail".into(),
            description: "Email, calendar and contacts".into(),
            url: config.mail_url.clone(),
        });
    }

    // Helpdesk — only if explicitly configured (not empty)
    if !config.ticketing_url.is_empty() {
        services.push(Service {
            id: "support".into(),
            name: "Support".into(),
            description: "Helpdesk — tickets and knowledge base".into(),
            url: config.ticketing_url.clone(),
        });
    }

    // Website CMS — only if explicitly configured (not empty)
    if !config.cms_url.is_empty() {
        services.push(Service {
            id: "website".into(),
            name: "Website".into(),
            description: "Content management — pages, news and blog".into(),
            url: config.cms_url.clone(),
        });
    }

    // Store — only if explicitly configured (not empty)
    if !config.shop_url.is_empty() {
        services.push(Service {
            id: "shop".into(),
            name: "Shop".into(),
            description: "Storefront — products, cart and checkout".into(),
            url: config.shop_url.clone(),
        });
    }

    // Workflow (Operaton) — only if explicitly configured (not empty)
    if !config.bpm_url.is_empty() {
        services.push(Service {
            id: "workflow".into(),
            name: "Workflow".into(),
            description: "BPMN process engine — Cockpit, Tasklist and REST API".into(),
            url: config.bpm_url.clone(),
        });
    }

    // Invoice (Invoice Ninja) — only if explicitly configured (not empty)
    if !config.invoice_url.is_empty() {
        services.push(Service {
            id: "invoice".into(),
            name: "Invoice".into(),
            description: "Invoicing — quotes, invoices and payments".into(),
            url: config.invoice_url.clone(),
        });
    }

    // Archive (Paperless-ngx) — only if explicitly configured (not empty)
    if !config.paperless_url.is_empty() {
        services.push(Service {
            id: "archive".into(),
            name: "Archive".into(),
            description: "Document archive — full-text searchable records".into(),
            url: config.paperless_url.clone(),
        });
    }

    // Chat (Element/Synapse) — only if explicitly configured (not empty)
    if !config.chat_url.is_empty() {
        services.push(Service {
            id: "chat".into(),
            name: "Chat".into(),
            description: "Team messaging — channels and direct messages".into(),
            url: config.chat_url.clone(),
        });
    }

    // Notes (Impress) — only if explicitly configured (not empty)
    if !config.notes_url.is_empty() {
        services.push(Service {
            id: "notes".into(),
            name: "Notes".into(),
            description: "Shared notes — links and snippets for the team".into(),
            url: config.notes_url.clone(),
        });
    }

    // Surveys (LimeSurvey CE) — only if explicitly configured (not empty)
    if !config.surveys_url.is_empty() {
        services.push(Service {
            id: "surveys".into(),
            name: "Surveys".into(),
            description: "Online surveys — LimeSurvey".into(),
            url: config.surveys_url.clone(),
        });
    }

    services
}

async fn handle_root(State(config): State<Arc<AppConfig>>) -> impl IntoResponse {
    let html = build_landing_page(&config);
    Html(html)
}

async fn handle_health() -> impl IntoResponse {
    Json(serde_json::json!({"status": "ok"}))
}

/// Prometheus scrape endpoint (text exposition format).
async fn handle_metrics() -> impl IntoResponse {
    match metrics::global().render() {
        Ok(body) => (
            StatusCode::OK,
            [(
                axum::http::header::CONTENT_TYPE,
                "text/plain; version=0.0.4; charset=utf-8",
            )],
            body,
        ),
        Err(e) => {
            warn!(%e, "metrics render failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                [(
                    axum::http::header::CONTENT_TYPE,
                    "text/plain; charset=utf-8",
                )],
                format!("metrics gather error: {e}"),
            )
        }
    }
}

async fn handle_services(State(config): State<Arc<AppConfig>>) -> impl IntoResponse {
    let services = get_services(&config);
    Json(serde_json::json!({ "services": services }))
}

async fn handle_announcements(State(config): State<Arc<AppConfig>>) -> impl IntoResponse {
    Json(serde_json::json!({ "announcements": config.announcements.as_ref() }))
}

/// Same-origin news columns: server-side RSS aggregation (see feeds.rs).
/// Always 200 — a broken upstream yields an empty column, never an error.
async fn handle_feeds(State(config): State<Arc<AppConfig>>) -> impl IntoResponse {
    Json(serde_json::json!({ "columns": config.feed_fetcher.snapshot().await }))
}

#[derive(Deserialize)]
struct AiChatRequest {
    question: String,
}

#[derive(Deserialize)]
struct ChatChoiceMessage {
    content: Option<String>,
}

#[derive(Deserialize)]
struct ChatChoice {
    message: Option<ChatChoiceMessage>,
}

#[derive(Deserialize)]
struct ChatResponse {
    choices: Vec<ChatChoice>,
}

/// Same-origin proxy to the operator-configured OpenAI-compatible endpoint.
/// Only mounted when AI_API_URL is set (card and endpoint stay hidden
/// otherwise). Stateless by design: one question in, one answer out.
async fn handle_ai_chat(
    State(config): State<Arc<AppConfig>>,
    Json(req): Json<AiChatRequest>,
) -> impl IntoResponse {
    let Some(ai) = config.ai.as_ref() else {
        return (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({
                "error": "AI integration not configured"
            })),
        );
    };

    let question = req.question.trim().to_string();
    if question.is_empty() || question.len() > 4000 {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "question must be 1-4000 chars" })),
        );
    }

    let url = format!("{}/v1/chat/completions", ai.api_url.trim_end_matches('/'));
    let mut builder = config.ai_client.post(&url).json(&serde_json::json!({
        "model": ai.model,
        "messages": [{ "role": "user", "content": question }],
        "max_tokens": 512,
    }));
    if let Some(key) = ai.api_key.as_deref().filter(|k| !k.is_empty()) {
        builder = builder.bearer_auth(key);
    }

    match builder.send().await {
        Ok(resp) if resp.status().is_success() => match resp.json::<ChatResponse>().await {
            Ok(body) => {
                let answer = body
                    .choices
                    .first()
                    .and_then(|c| c.message.as_ref())
                    .and_then(|m| m.content.clone())
                    .unwrap_or_default();
                (
                    StatusCode::OK,
                    Json(serde_json::json!({ "answer": answer })),
                )
            }
            Err(e) => (
                StatusCode::BAD_GATEWAY,
                Json(serde_json::json!({ "error": format!("upstream decode error: {e}") })),
            ),
        },
        Ok(resp) => (
            StatusCode::BAD_GATEWAY,
            Json(serde_json::json!({
                "error": format!("upstream returned {}", resp.status())
            })),
        ),
        Err(e) => (
            StatusCode::BAD_GATEWAY,
            Json(serde_json::json!({ "error": format!("upstream unreachable: {e}") })),
        ),
    }
}

/// Security headers on every response (belt-and-braces with the CSP meta
/// tag in the HTML — headers also cover the JSON API responses).
async fn security_headers(
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let mut res = next.run(request).await;
    let h = res.headers_mut();
    h.insert("x-content-type-options", "nosniff".parse().unwrap());
    h.insert("x-frame-options", "DENY".parse().unwrap());
    // HSTS: the portal is served TLS-only (traefik redirects :80 → :443),
    // so telling browsers to refuse plain-http for a year is always safe
    // here — and it covers the JSON API responses too.
    h.insert(
        "strict-transport-security",
        "max-age=31536000; includeSubDomains".parse().unwrap(),
    );
    h.insert(
        "referrer-policy",
        "strict-origin-when-cross-origin".parse().unwrap(),
    );
    h.insert(
        "content-security-policy",
        "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; script-src 'self'; connect-src 'self'"
            .parse()
            .unwrap(),
    );
    res
}

/// Prometheus instrumentation for every served request: one counter
/// increment + one latency observation, labelled by method, matched route
/// and status code (see metrics.rs).
async fn track_metrics(
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let start = std::time::Instant::now();
    let method = request.method().to_string();
    // MatchedPath is populated by routing before router-level middleware
    // runs, so the route label is the route pattern, not the raw URI.
    let route = request
        .extensions()
        .get::<axum::extract::MatchedPath>()
        .map(|p| p.as_str().to_string())
        .unwrap_or_else(|| request.uri().path().to_string());
    let response = next.run(request).await;
    metrics::global().record_request(
        &method,
        &route,
        response.status().as_u16(),
        start.elapsed().as_secs_f64(),
    );
    response
}

fn build_router(config: Arc<AppConfig>) -> Router {
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    let mut router = Router::new()
        .route("/", get(handle_root))
        .route("/app.js", get(handle_app_js))
        .route("/health", get(handle_health))
        .route("/api/services", get(handle_services))
        .route("/api/announcements", get(handle_announcements))
        .route("/api/feeds", get(handle_feeds))
        .route("/api/intercom", get(handle_intercom_list))
        .route("/api/intercom", post(handle_intercom_post))
        .route("/api/intercom/upload", post(handle_intercom_upload))
        .route(
            "/api/intercom/files/:token/:name",
            get(handle_intercom_file),
        )
        .route("/api/logout", post(logout::handle_logout))
        .route("/logged-out", get(logout::handle_logged_out));

    if config.ai.is_some() {
        router = router.route("/api/ai/chat", post(handle_ai_chat));
    }

    if config.polls.is_some() {
        router = router
            .route("/api/polls", get(handle_polls_list))
            .route("/api/polls", post(handle_polls_create))
            .route("/api/polls/upload", post(handle_polls_upload))
            .route("/api/polls/:id/vote", post(handle_polls_vote))
            .route("/api/files/:token/:name", get(handle_polls_file));
    }

    router
        .route("/metrics", get(handle_metrics))
        .layer(middleware::from_fn(security_headers))
        .layer(middleware::from_fn(track_metrics))
        .layer(cors)
        .with_state(config)
}

/// Portal intercom: a short internal note, optionally referencing one
/// attachment from the cloud service (metadata snapshot only).
#[derive(Deserialize)]
struct IntercomPost {
    text: String,
    #[serde(default)]
    attachment_url: Option<String>,
}

async fn handle_intercom_list(State(config): State<Arc<AppConfig>>) -> impl IntoResponse {
    Json(serde_json::json!({ "messages": config.intercom_store.list() }))
}

/// Team-notes persistence layout: INTERCOM_DATA_PATH holds the JSON store,
/// picked-file uploads live in "attachments/" next to it.
fn intercom_store_from_env() -> intercom::IntercomStore {
    match std::env::var("INTERCOM_DATA_PATH") {
        Ok(path) if !path.trim().is_empty() => {
            let path = PathBuf::from(path.trim());
            let dir = path.parent().map(|p| p.to_path_buf()).unwrap_or_default();
            let attachments = dir.join("attachments");
            if let Err(e) = std::fs::create_dir_all(&attachments) {
                // Usually a volume ownership problem (portal runs as uid 1000);
                // notes still work, picked-file uploads will 400 until fixed.
                tracing::warn!(%e, dir = %attachments.display(), "cannot create intercom attachments dir — file picker uploads will fail");
            }
            intercom::IntercomStore::open(Some(path), Some(attachments))
        }
        _ => intercom::IntercomStore::default(),
    }
}

#[derive(Deserialize)]
struct IntercomUploadQuery {
    text: String,
    #[serde(default)]
    filename: Option<String>,
}

/// File-picker upload: raw body (no multipart feature), metadata in the
/// query. Posts the note directly with the stored attachment.
async fn handle_intercom_upload(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Query(q): axum::extract::Query<IntercomUploadQuery>,
    headers: axum::http::HeaderMap,
    bytes: axum::body::Bytes,
) -> impl IntoResponse {
    let bad = |msg: &str| {
        (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": msg })),
        )
    };
    let text = match intercom::validate_text(&q.text) {
        Ok(t) => t,
        Err(e) => return bad(&e).into_response(),
    };
    let filename = q
        .filename
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .or_else(|| {
            headers
                .get(axum::http::header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                .and_then(|ct| ct.rsplit('/').next())
                .map(|ext| format!("upload.{ext}"))
        })
        .unwrap_or_else(|| "upload.bin".into());
    let content_type = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .split(';')
        .next()
        .unwrap_or("application/octet-stream")
        .trim()
        .to_string();
    let (token, name) = match config.intercom_store.save_attachment(&filename, &bytes) {
        Ok(pair) => pair,
        Err(e) => return bad(&e).into_response(),
    };
    let attachment = intercom::Attachment {
        url: format!("/api/intercom/files/{token}/{name}"),
        name,
        size: Some(bytes.len() as u64),
        content_type,
    };
    let msg = config.intercom_store.push(text, Some(attachment));
    metrics::global().inc_intercom_message();
    (
        StatusCode::CREATED,
        Json(serde_json::json!({ "message": msg })),
    )
        .into_response()
}

/// Serve a picked-file attachment. Body from disk, content-type from the
/// stored snapshot; never trusts the request path beyond token+name.
async fn handle_intercom_file(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Path((token, name)): axum::extract::Path<(String, String)>,
) -> impl IntoResponse {
    use axum::http::header::{CONTENT_DISPOSITION, CONTENT_TYPE};
    match config.intercom_store.read_attachment(&token, &name) {
        Some((bytes, ct)) => (
            [
                (CONTENT_TYPE, ct),
                (CONTENT_DISPOSITION, format!("inline; filename=\"{name}\"")),
            ],
            bytes,
        )
            .into_response(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

// ── Polls (Umfragen) — optional feature behind POLLS_ENABLED=1 ────────

fn polls_store_from_env() -> Option<Arc<polls::PollStore>> {
    let enabled = std::env::var("POLLS_ENABLED")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    if !enabled {
        return None;
    }
    let path = std::env::var("POLLS_DATA_PATH")
        .ok()
        .map(|p| p.trim().to_string())
        .filter(|p| !p.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/data/polls.json"));
    let dir = path.parent().map(|p| p.to_path_buf()).unwrap_or_default();
    let attachments = dir.join("attachments");
    if let Err(e) = std::fs::create_dir_all(&attachments) {
        tracing::warn!(%e, dir = %attachments.display(), "cannot create polls attachments dir — local uploads will fail");
    }
    let cloud = polls::CloudUploadConfig::from_env();
    tracing::info!(cloud = cloud.is_some(), "polls feature enabled");
    Some(Arc::new(polls::PollStore::open(
        Some(path),
        Some(attachments),
        cloud,
    )))
}

#[derive(Deserialize)]
struct PollPost {
    question: String,
    options: Vec<String>,
    /// Cloud share link (https://cloud…/s/…) — metadata snapshot only.
    #[serde(default)]
    attachment_url: Option<String>,
}

#[derive(Deserialize)]
struct PollVotePost {
    option: usize,
}

#[derive(Deserialize)]
struct PollUploadQuery {
    question: String,
    /// JSON-encoded array of option strings (client uses encodeURIComponent).
    options: String,
    #[serde(default)]
    filename: Option<String>,
    /// Cloud share link — alternative to a picked-file upload body.
    #[serde(default)]
    attachment_url: Option<String>,
}

async fn handle_polls_list(State(config): State<Arc<AppConfig>>) -> impl IntoResponse {
    match &config.polls {
        Some(store) => Json(serde_json::json!({ "polls": store.list() })).into_response(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

async fn handle_polls_create(
    State(config): State<Arc<AppConfig>>,
    Json(post): Json<PollPost>,
) -> impl IntoResponse {
    let Some(store) = &config.polls else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let bad = |msg: &str| {
        (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": msg})),
        )
    };
    let (question, options) = match polls::validate_poll(&post.question, &post.options) {
        Ok(pair) => pair,
        Err(e) => return bad(&e).into_response(),
    };
    let attachment =
        match resolve_cloud_attachment(&config, post.attachment_url.as_deref().unwrap_or("")).await
        {
            Ok(a) => a,
            Err(e) => return bad(&e).into_response(),
        };
    let poll = store.create(question, options, attachment);
    (
        StatusCode::CREATED,
        Json(serde_json::json!({ "poll": poll })),
    )
        .into_response()
}

/// Create a poll carrying a picked-file attachment (raw body upload).
/// With CLOUD_WEBDAV_* configured the file goes to the cloud (WebDAV PUT
/// + public share link); otherwise it falls back to the local dir.
async fn handle_polls_upload(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Query(q): axum::extract::Query<PollUploadQuery>,
    headers: axum::http::HeaderMap,
    bytes: axum::body::Bytes,
) -> impl IntoResponse {
    let Some(store) = &config.polls else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let bad = |msg: &str| {
        (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": msg})),
        )
    };
    let options: Vec<String> = match serde_json::from_str(&q.options) {
        Ok(v) => v,
        Err(_) => return bad("options must be a JSON array string").into_response(),
    };
    let (question, options) = match polls::validate_poll(&q.question, &options) {
        Ok(pair) => pair,
        Err(e) => return bad(&e).into_response(),
    };
    let attachment = if let Some(url) = q
        .attachment_url
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        match resolve_cloud_attachment(&config, url).await {
            Ok(a) => a,
            Err(e) => return bad(&e).into_response(),
        }
    } else if bytes.is_empty() {
        None
    } else {
        let filename = q
            .filename
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("upload.bin")
            .to_string();
        match &store.cloud {
            Some(cloud) => {
                let key = intercom::random_token();
                match polls::upload_to_cloud(
                    cloud,
                    &config.intercom_client,
                    &key,
                    &filename,
                    &bytes,
                )
                .await
                {
                    Ok(a) => Some(a),
                    Err(e) => return bad(&e).into_response(),
                }
            }
            None => {
                let content_type = headers
                    .get(axum::http::header::CONTENT_TYPE)
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("application/octet-stream")
                    .split(';')
                    .next()
                    .unwrap_or("application/octet-stream")
                    .trim()
                    .to_string();
                match store.save_attachment_local(&filename, &bytes) {
                    Ok((token, name)) => Some(intercom::Attachment {
                        url: format!("/api/files/{token}/{name}"),
                        name,
                        size: Some(bytes.len() as u64),
                        content_type,
                    }),
                    Err(e) => return bad(&e).into_response(),
                }
            }
        }
    };
    let poll = store.create(question, options, attachment);
    (
        StatusCode::CREATED,
        Json(serde_json::json!({ "poll": poll })),
    )
        .into_response()
}

async fn handle_polls_vote(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Path(id): axum::extract::Path<u64>,
    Json(post): Json<PollVotePost>,
) -> impl IntoResponse {
    let Some(store) = &config.polls else {
        return StatusCode::NOT_FOUND.into_response();
    };
    match store.vote(id, post.option) {
        Ok(poll) => Json(serde_json::json!({ "poll": poll })).into_response(),
        Err(e) => (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": e})),
        )
            .into_response(),
    }
}

/// Serve a local-fallback poll attachment (same guards as intercom).
async fn handle_polls_file(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Path((token, name)): axum::extract::Path<(String, String)>,
) -> impl IntoResponse {
    use axum::http::header::{CONTENT_DISPOSITION, CONTENT_TYPE};
    let Some(store) = &config.polls else {
        return StatusCode::NOT_FOUND.into_response();
    };
    match store.read_attachment_local(&token, &name) {
        Some((bytes, ct)) => (
            [
                (CONTENT_TYPE, ct),
                (CONTENT_DISPOSITION, format!("inline; filename=\"{name}\"")),
            ],
            bytes,
        )
            .into_response(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

/// Resolve an "attachment from the cloud" URL into a metadata snapshot:
/// SSRF/allowlist guards + HEAD + Content-Disposition/Length/Type. The
/// file itself stays in the cloud — the portal never mediates bodies.
async fn resolve_cloud_attachment(
    config: &AppConfig,
    raw: &str,
) -> Result<Option<intercom::Attachment>, String> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(None);
    }
    let url = intercom::validate_attachment_url(raw, &config.intercom)?;
    let head = config
        .intercom_client
        .head(&url)
        .timeout(Duration::from_secs(10))
        .send()
        .await;
    let head = match head {
        Ok(r) if r.status().is_success() => r,
        Ok(r) => {
            return Err(format!(
                "attachment source answered HTTP {}",
                r.status().as_u16()
            ))
        }
        Err(_) => return Err("attachment source unreachable".to_string()),
    };
    let hdr = |name: &axum::http::HeaderName| {
        head.headers()
            .get(name)
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string())
    };
    let size = head
        .headers()
        .get(axum::http::header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.parse().ok());
    Ok(Some(intercom::parse_attachment_metadata(
        &url,
        hdr(&axum::http::header::CONTENT_TYPE).as_deref(),
        size,
        hdr(&axum::http::header::CONTENT_DISPOSITION).as_deref(),
    )))
}

async fn handle_intercom_post(
    State(config): State<Arc<AppConfig>>,
    Json(post): Json<IntercomPost>,
) -> impl IntoResponse {
    use axum::response::IntoResponse;

    let bad = |msg: &str| {
        (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": msg })),
        )
    };

    let text = match intercom::validate_text(&post.text) {
        Ok(t) => t,
        Err(e) => return bad(&e).into_response(),
    };

    let attachment =
        match resolve_cloud_attachment(&config, post.attachment_url.as_deref().unwrap_or("")).await
        {
            Ok(a) => a,
            Err(e) => return bad(&e).into_response(),
        };

    let msg = config.intercom_store.push(text, attachment);
    metrics::global().inc_intercom_message();
    (
        StatusCode::CREATED,
        Json(serde_json::json!({ "message": msg })),
    )
        .into_response()
}

async fn handle_app_js() -> impl IntoResponse {
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "application/javascript; charset=utf-8",
        )],
        include_str!("app.js"),
    )
}

async fn shutdown_signal() {
    let ctrl_c = async {
        signal::ctrl_c()
            .await
            .expect("failed to install Ctrl+C handler");
    };

    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }

    info!("shutdown signal received, starting graceful shutdown");
}

fn init_logging() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "opensme_portal=info,tower_http=info".into()),
        )
        .init();
}

fn load_config() -> Arc<AppConfig> {
    let ai_api_url = load_env("AI_API_URL", "");
    let ai = if ai_api_url.is_empty() {
        None
    } else {
        Some(AiConfig {
            api_url: ai_api_url,
            model: load_env("AI_MODEL", ""),
            api_key: std::env::var("AI_API_KEY").ok(),
        })
    };

    let announcements = Arc::new(parse_announcements(&load_env("PORTAL_ANNOUNCEMENTS", "")));

    Arc::new(AppConfig {
        portal_domain: load_env("PORTAL_DOMAIN", "portal.opensme.graphwiz.ai"),
        opensme_domain: load_env("OPENSME_DOMAIN", "opensme.graphwiz.ai"),
        opencloud_url: load_env("OPENCLOUD_URL", "https://cloud.opensme.graphwiz.ai"),
        mail_url: load_env("MAIL_URL", ""),
        idp_url: load_env("IDP_URL", ""),
        ticketing_url: load_env("TICKETING_URL", ""),
        cms_url: load_env("CMS_URL", ""),
        shop_url: load_env("SHOP_URL", ""),
        bpm_url: load_env("BPM_URL", ""),
        invoice_url: load_env("INVOICE_URL", ""),
        paperless_url: load_env("PAPERLESS_URL", ""),
        chat_url: load_env("CHAT_URL", ""),
        notes_url: load_env("NOTES_URL", ""),
        surveys_url: load_env("SURVEYS_URL", ""),
        announcements,
        ai,
        // One shared client: connection pooling, 30s default timeout.
        // 300s: CPU-served LLMs cold-load for minutes — the first chat
        // after ollama eviction must not surface as "upstream unreachable".
        ai_client: reqwest::Client::builder()
            .timeout(Duration::from_secs(300))
            .build()
            .unwrap_or_default(),
        intercom: intercom::IntercomConfig::from_env(&load_env(
            "OPENSME_DOMAIN",
            "opensme.graphwiz.ai",
        )),
        intercom_store: Arc::new(intercom_store_from_env()),
        polls: polls_store_from_env(),
        intercom_client: {
            let mut builder = reqwest::Client::builder().timeout(Duration::from_secs(15));
            // Egress-restricted hosts reach the cloud (attachment metadata
            // HEAD, poll uploads) only through the egress proxy — same
            // wiring as the feed client.
            if let Some(mut proxy) = feeds::proxy_from_env() {
                // Internal targets (docker-net names like opensme-opencloud)
                // must bypass the egress proxy: it cannot resolve bridge
                // names and would fail every internal http request.
                if let Some(np) = std::env::var("PORTAL_NO_PROXY")
                    .ok()
                    .map(|v| v.trim().to_string())
                    .filter(|v| !v.is_empty())
                {
                    proxy = proxy.no_proxy(reqwest::NoProxy::from_string(&np));
                }
                builder = builder.proxy(proxy);
            }
            builder.build().unwrap_or_default()
        },
        feed_fetcher: Arc::new(feeds::FeedFetcher::new(feeds::parse_columns(&load_env(
            "PORTAL_FEEDS",
            "",
        )))),
    })
}

#[tokio::main]
async fn main() {
    init_logging();

    let config = load_config();

    info!(
        portal_domain = %config.portal_domain,
        opensme_domain = %config.opensme_domain,
        ai_enabled = config.ai.is_some(),
        announcements = config.announcements.len(),
        polls_enabled = config.polls.is_some(),
        feed_columns = config.feed_fetcher.columns().len(),
        "starting opensme-portal"
    );

    let app = build_router(config);

    let port: u16 = std::env::var("PORTAL_PORT")
        .ok()
        .and_then(|p| p.trim().parse().ok())
        .unwrap_or(8080);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .unwrap_or_else(|e| {
            panic!("failed to bind to {addr}: {e}");
        });

    info!("listening on {addr}");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .unwrap_or_else(|e| {
            panic!("server error: {e}");
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    pub(crate) fn cfg_with(
        ticketing: &str,
        ai: Option<AiConfig>,
        announcements: &str,
    ) -> AppConfig {
        AppConfig {
            opencloud_url: "https://cloud.example".into(),
            mail_url: String::new(),
            idp_url: "https://auth.example".into(),
            ticketing_url: ticketing.into(),
            cms_url: String::new(),
            shop_url: String::new(),
            bpm_url: String::new(),
            invoice_url: String::new(),
            paperless_url: String::new(),
            chat_url: String::new(),
            notes_url: String::new(),
            surveys_url: String::new(),
            portal_domain: "portal.example".into(),
            opensme_domain: "example".into(),
            announcements: Arc::new(parse_announcements(announcements)),
            ai,
            ai_client: reqwest::Client::new(),
            intercom: intercom::IntercomConfig {
                allowed_hosts: vec!["cloud.example".into()],
                allow_http: false,
            },
            intercom_store: Arc::new(intercom::IntercomStore::default()),
            polls: None,
            intercom_client: reqwest::Client::new(),
            feed_fetcher: Arc::new(feeds::FeedFetcher::new(feeds::parse_columns(""))),
        }
    }

    #[test]
    fn announcements_empty_for_unset_or_blank() {
        assert!(parse_announcements("").is_empty());
        assert!(parse_announcements("   ").is_empty());
    }

    #[test]
    fn announcements_parse_valid() {
        let raw = r#"[{"level":"warn","text":"Maintenance tonight"},{"text":"Welcome"}]"#;
        let got = parse_announcements(raw);
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].level, "warn");
        assert_eq!(got[1].level, "info"); // default level
    }

    #[test]
    fn announcements_invalid_json_ignored() {
        assert!(parse_announcements("not json").is_empty());
        assert!(parse_announcements(r#"{"level":"info"}"#).is_empty()); // not an array
    }

    #[test]
    fn announcements_capped_and_blank_filtered() {
        let mut raw = String::from("[");
        for i in 0..20 {
            if i > 0 {
                raw.push_str(", ");
            }
            raw.push_str(&format!("{{\"text\":\"m{i}\"}}"));
        }
        raw.push_str(", {\"text\":\"   \"}]"); // 20 valid + 1 blank
        assert_eq!(parse_announcements(&raw).len(), MAX_ANNOUNCEMENTS);
    }

    #[test]
    fn announcements_rendered_escaped() {
        let a = Announcement {
            level: "info".into(),
            text: "<script>alert(1)</script> & \"quotes\"".into(),
        };
        let html = render_announcements(&[a]);
        assert!(html.contains("&lt;script&gt;"));
        assert!(!html.contains("<script>"));
        assert!(html.contains("&amp;"));
        assert!(html.contains("&quot;"));
    }

    #[test]
    fn ticketing_card_gated_on_url() {
        let with = get_services(&cfg_with("https://help.example", None, ""));
        assert!(with.iter().any(|s| s.name == "Support"));

        let without = get_services(&cfg_with("", None, ""));
        assert!(!without.iter().any(|s| s.name == "Support"));
    }

    #[test]
    fn cms_and_shop_cards_gated_on_url() {
        let mut c = cfg_with("", None, "");
        assert!(!get_services(&c).iter().any(|s| s.name == "Website"));
        assert!(!get_services(&c).iter().any(|s| s.name == "Shop"));

        c.cms_url = "https://www.example".into();
        c.shop_url = "https://shop.example".into();
        let services = get_services(&c);
        assert!(services.iter().any(|s| s.name == "Website"));
        assert!(services.iter().any(|s| s.name == "Shop"));
    }

    #[test]
    fn bpm_card_gated_on_url() {
        let mut c = cfg_with("", None, "");
        assert!(!get_services(&c).iter().any(|s| s.name == "Workflow"));

        c.bpm_url = "https://bpm.example".into();
        let services = get_services(&c);
        assert!(services.iter().any(|s| s.name == "Workflow"));
        assert!(services.iter().any(|s| s.description.contains("BPMN")));
    }

    #[test]
    fn identity_and_collabora_never_tiled() {
        // Infrastructure exclusion (portal-catalog spec): auth is SSO
        // plumbing (idp_url may be set for redirects), Collabora is an
        // embedded editor inside OpenCloud — neither ever becomes a tile.
        let c = cfg_with("", None, "");
        let services = get_services(&c);
        assert!(!services.iter().any(|s| s.id == "identity"));
        assert!(!services.iter().any(|s| s.id == "collabora"));
        assert!(!services.iter().any(|s| s.name == "Identity"));
        assert!(!services.iter().any(|s| s.name == "Collabora"));
    }

    #[test]
    fn invoice_archive_chat_notes_gated_on_url() {
        let mut c = cfg_with("", None, "");
        for name in ["Invoice", "Archive", "Chat", "Notes"] {
            assert!(!get_services(&c).iter().any(|s| s.name == name));
        }

        c.invoice_url = "https://invoice.example".into();
        c.paperless_url = "https://docs.example".into();
        c.chat_url = "https://chat.example".into();
        c.notes_url = "https://notes.example".into();
        let services = get_services(&c);
        assert!(services.iter().any(|s| s.name == "Invoice"));
        assert!(services.iter().any(|s| s.name == "Archive"));
        assert!(services.iter().any(|s| s.name == "Chat"));
        assert!(services.iter().any(|s| s.name == "Notes"));
    }

    #[test]
    fn service_ids_are_stable_unique_and_complete() {
        // Fully-configured portal: every card carries a stable id.
        let c = AppConfig {
            mail_url: "https://mail.example".into(),
            ticketing_url: "https://help.example".into(),
            cms_url: "https://www.example".into(),
            shop_url: "https://shop.example".into(),
            bpm_url: "https://bpm.example".into(),
            invoice_url: "https://invoice.example".into(),
            paperless_url: "https://docs.example".into(),
            chat_url: "https://chat.example".into(),
            notes_url: "https://notes.example".into(),
            surveys_url: "https://grist.example".into(),
            ..cfg_with("", None, "")
        };
        let services = get_services(&c);
        assert_eq!(services.len(), 11, "all eleven catalog cards configured");

        let ids: Vec<&str> = services.iter().map(|s| s.id.as_str()).collect();
        assert!(
            ids.iter().all(|id| !id.is_empty()),
            "every service needs an id: {ids:?}"
        );
        let unique: std::collections::HashSet<&str> = ids.iter().copied().collect();
        assert_eq!(
            unique.len(),
            ids.len(),
            "service ids must be unique: {ids:?}"
        );
        assert!(ids.contains(&"opencloud"));
        assert!(ids.contains(&"webmail"));
        assert!(ids.contains(&"workflow"));
        assert!(ids.contains(&"invoice"));
        assert!(ids.contains(&"archive"));
        assert!(ids.contains(&"chat"));
        assert!(ids.contains(&"notes"));
        assert!(ids.contains(&"surveys"));
        assert!(!ids.contains(&"identity"));
        assert!(!ids.contains(&"collabora"));
    }

    #[test]
    fn ai_card_and_page_wiring() {
        let ai = AiConfig {
            api_url: "http://vllm:8000".into(),
            model: "test-model".into(),
            api_key: None,
        };
        let html = build_landing_page(&cfg_with("", Some(ai.clone()), ""));
        assert!(html.contains("id=\"ai-card\""));

        let plain = build_landing_page(&cfg_with("", None, ""));
        assert!(!plain.contains("id=\"ai-card\""));
    }

    #[test]
    fn announcements_rendered_into_page() {
        let html = build_landing_page(&cfg_with(
            "",
            None,
            r#"[{"level":"warn","text":"planned outage"}]"#,
        ));
        assert!(html.contains("class=\"announcement warn\""));
        assert!(html.contains("planned outage"));
    }

    #[test]
    fn fast_switch_marker_on_cards() {
        let html = build_landing_page(&cfg_with("", None, ""));
        assert!(html.contains("data-pal="));
        assert!(html.contains("/app.js"));
    }

    #[test]
    fn head_carries_favicon_and_meta() {
        let html = build_landing_page(&cfg_with("", None, ""));
        // Inline SVG favicon (data URI — CSP allows img-src data:)
        assert!(html.contains("rel=\"icon\""));
        assert!(html.contains("data:image/svg+xml"));
        // Social / search metadata
        assert!(html.contains("name=\"description\""));
        assert!(html.contains("og:title"));
        assert!(html.contains("name=\"theme-color\""));
    }

    #[test]
    fn html_escape_covers_angle_brackets() {
        assert_eq!(html_escape("<b>"), "&lt;b&gt;");
    }

    #[test]
    fn landing_page_mentions_opensme() {
        let html = build_landing_page(&cfg_with("", None, ""));
        assert!(html.to_lowercase().contains("opensme"));
        assert!(html.contains("lang=\"de\""));
    }

    #[test]
    fn landing_page_renders_feed_cards_per_configured_column() {
        let html = build_landing_page(&cfg_with("", None, ""));
        // Default columns are rendered server-side; the client fills the
        // lists from /api/feeds (no network needed for the shell).
        assert!(html.contains("id=\"feed-startup\""));
        assert!(html.contains("id=\"feed-markt\""));
        assert!(html.contains("id=\"feed-legal\""));
        assert!(html.contains(">Startup</h2>"));
        assert!(html.contains("Aktuelles"));
        assert!(html.contains("Lade Meldungen"));
        assert!(html.contains("func-card"));
    }

    #[tokio::test]
    async fn feeds_api_returns_columns_even_when_upstreams_dead() {
        // FeedFetcher pointed at a dead address: the endpoint must still
        // return 200 with the configured column shape (empty entries),
        // never a 5xx — a broken upstream must not break the page.
        let mut cfg = cfg_with("", None, "");
        cfg.feed_fetcher = Arc::new(feeds::FeedFetcher::new(vec![feeds::FeedColumn {
            id: "test".into(),
            title: "Test".into(),
            urls: vec!["http://127.0.0.1:1/rss".into()],
        }]));
        let resp = respond(cfg, "GET", "/api/feeds", None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let body = body_json(resp).await;
        let cols = body["columns"].as_array().expect("columns array");
        assert_eq!(cols.len(), 1);
        assert_eq!(cols[0]["id"], "test");
        assert_eq!(cols[0]["entries"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn intercom_card_always_on_page() {
        let html = build_landing_page(&cfg_with("", None, ""));
        assert!(html.contains("id=\"intercom-card\""));
        assert!(html.contains("ic-send"));
    }

    #[test]
    fn intercom_messages_rendered_escaped_into_page() {
        let cfg = cfg_with("", None, "");
        cfg.intercom_store
            .push("note <script>alert(1)</script>".into(), None);
        let att = intercom::parse_attachment_metadata(
            "https://cloud.example/s/f.pdf",
            Some("application/pdf"),
            Some(2048),
            Some("attachment; filename=\"Rechnung<script>.pdf\""),
        );
        cfg.intercom_store.push("see attached".into(), Some(att));
        let html = build_landing_page(&cfg);
        assert!(!html.contains("<script>alert(1)"));
        assert!(html.contains("&lt;script&gt;alert(1)&lt;/script&gt;"));
        assert!(html.contains("Rechnung&lt;script&gt;.pdf"));
        assert!(html.contains("2.0 KB"));
        assert!(html.contains("application/pdf"));
    }

    // ── In-process HTTP contract tests (tower oneshot — no sockets) ────
    use axum::body::Body;
    use http_body_util::BodyExt;
    use std::sync::Mutex;
    use tower::ServiceExt;

    async fn respond(
        cfg: AppConfig,
        method: &str,
        uri: &str,
        json: Option<serde_json::Value>,
    ) -> axum::http::Response<Body> {
        let router = build_router(Arc::new(cfg));
        let builder = axum::http::Request::builder().method(method).uri(uri);
        let request = match json {
            Some(v) => builder
                .header("content-type", "application/json")
                .body(Body::from(v.to_string()))
                .unwrap(),
            None => builder.body(Body::empty()).unwrap(),
        };
        router.oneshot(request).await.unwrap()
    }

    async fn body_json(resp: axum::http::Response<Body>) -> serde_json::Value {
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        serde_json::from_slice(&bytes).expect("response body is JSON")
    }

    fn header(resp: &axum::http::Response<Body>, name: &str) -> String {
        resp.headers()
            .get(name)
            .expect("expected header present")
            .to_str()
            .map(str::to_string)
            .unwrap()
    }

    #[tokio::test]
    async fn root_serves_security_headers() {
        let resp = respond(cfg_with("", None, ""), "GET", "/", None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(header(&resp, "x-content-type-options"), "nosniff");
        assert_eq!(header(&resp, "x-frame-options"), "DENY");
        assert_eq!(
            header(&resp, "referrer-policy"),
            "strict-origin-when-cross-origin"
        );
        let csp = header(&resp, "content-security-policy");
        assert!(csp.contains("default-src 'self'"));
        let hsts = header(&resp, "strict-transport-security");
        assert!(hsts.contains("max-age=31536000"), "hsts: {hsts}");
        assert!(hsts.contains("includeSubDomains"), "hsts: {hsts}");
    }

    #[tokio::test]
    async fn json_endpoints_carry_security_headers_too() {
        for uri in ["/health", "/api/services", "/api/announcements"] {
            let resp = respond(cfg_with("", None, ""), "GET", uri, None).await;
            assert_eq!(resp.status(), StatusCode::OK, "GET {uri}");
            assert_eq!(header(&resp, "x-content-type-options"), "nosniff", "{uri}");
        }
    }

    #[tokio::test]
    async fn ai_chat_route_absent_when_unconfigured() {
        // Route only mounted when AI_API_URL set — axum's default 404
        // (empty body) keeps the endpoint hidden.
        let resp = respond(
            cfg_with("", None, ""),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "ping" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn ai_chat_rejects_empty_and_oversized_questions() {
        // unroutable upstream — validation must short-circuit before any call
        let ai = AiConfig {
            api_url: "http://127.0.0.1:1".into(),
            model: "m".into(),
            api_key: None,
        };
        let resp = respond(
            cfg_with("", Some(ai.clone()), ""),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "   " })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);

        let resp = respond(
            cfg_with("", Some(ai), ""),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "x".repeat(4001) })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    /// Spawn a throwaway OpenAI-compatible upstream on an ephemeral port.
    /// Captures the Authorization header + model field of the last call.
    async fn spawn_mock_ai(
        status: StatusCode,
        payload: &'static str,
    ) -> (String, Arc<Mutex<Option<(String, String)>>>) {
        let captured: Arc<Mutex<Option<(String, String)>>> = Arc::new(Mutex::new(None));
        let cap = captured.clone();
        let app = axum::Router::new().route(
            "/v1/chat/completions",
            axum::routing::post(move |headers: axum::http::HeaderMap, body: String| {
                let cap = cap.clone();
                async move {
                    *cap.lock().unwrap() = Some((
                        headers
                            .get("authorization")
                            .and_then(|v| v.to_str().ok())
                            .map(str::to_string)
                            .unwrap_or_default(),
                        serde_json::from_str::<serde_json::Value>(&body)
                            .ok()
                            .and_then(|v| v["model"].as_str().map(str::to_string))
                            .unwrap_or_default(),
                    ));
                    (status, payload)
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (format!("http://{addr}"), captured)
    }

    fn cfg_with_ai(api_url: String) -> AppConfig {
        cfg_with(
            "",
            Some(AiConfig {
                api_url,
                model: "test-model".into(),
                api_key: Some("secret-key".into()),
            }),
            "",
        )
    }

    const PONG_JSON: &str = r#"{"choices":[{"message":{"content":"pong"}}]}"#;

    #[tokio::test]
    async fn ai_chat_happy_path_via_mock_upstream() {
        let (url, captured) = spawn_mock_ai(StatusCode::OK, PONG_JSON).await;
        let resp = respond(
            cfg_with_ai(url),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "  ping  " })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK);
        let body = body_json(resp).await;
        assert_eq!(body["answer"], "pong");

        let (auth, model) = captured.lock().unwrap().clone().unwrap();
        assert_eq!(auth, "Bearer secret-key"); // API key forwarded
        assert_eq!(model, "test-model"); // configured model forwarded
                                         // question was trimmed before proxying
    }

    #[tokio::test]
    async fn ai_chat_upstream_error_maps_to_502() {
        let (url, _) = spawn_mock_ai(StatusCode::INTERNAL_SERVER_ERROR, "boom").await;
        let resp = respond(
            cfg_with_ai(url),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "ping" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_GATEWAY);
        let body = body_json(resp).await;
        assert!(body["error"]
            .as_str()
            .unwrap()
            .contains("upstream returned 500"));
    }

    #[tokio::test]
    async fn ai_chat_upstream_bad_json_maps_to_502() {
        let (url, _) = spawn_mock_ai(StatusCode::OK, "<html>not json</html>").await;
        let resp = respond(
            cfg_with_ai(url),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "ping" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_GATEWAY);
        let body = body_json(resp).await;
        assert!(body["error"].as_str().unwrap().contains("decode error"));
    }

    // ── Property-based tests (proptest) ───────────────────────────────
    use proptest::prelude::*;

    proptest! {
        /// Any input string must parse without panicking, stay capped,
        /// normalize levels and drop blank texts.
        #[test]
        fn prop_announcements_parser_is_total(
            raw in ".*",
        ) {
            let got = parse_announcements(&raw);
            prop_assert!(got.len() <= MAX_ANNOUNCEMENTS);
            prop_assert!(got.iter().all(|a| a.level == "info" || a.level == "warn"));
            prop_assert!(got.iter().all(|a| !a.text.trim().is_empty()));
        }

        /// Escaped output must never contain raw angle brackets —
        // the XSS invariant, checked over arbitrary attacker input.
        #[test]
        fn prop_html_escape_strips_angle_brackets(s in ".*") {
            let escaped = html_escape(&s);
            prop_assert!(!escaped.contains('<'));
            prop_assert!(!escaped.contains('>'));
        }

        /// Parser vs. model: valid entries survive in order (capped),
        /// blanks are dropped, unknown levels normalize to info.
        #[test]
        fn prop_announcements_matches_model(
            items in proptest::collection::vec((proptest::bool::ANY, "[a-zA-Z0-9 ]{0,30}"), 0..12),
        ) {
            let raw = serde_json::to_string(&items.iter()
                .map(|(warn, text)| serde_json::json!({
                    "level": if *warn { "warn" } else { "curious" },
                    "text": text,
                }))
                .collect::<Vec<_>>()
            ).unwrap();
            let got = parse_announcements(&raw);
            let model: Vec<(bool, String)> = items.iter()
                .filter(|(_, t)| !t.trim().is_empty())
                .take(MAX_ANNOUNCEMENTS)
                .map(|(w, t)| (*w, t.clone()))
                .collect();
            prop_assert_eq!(got.len(), model.len());
            for (a, (warn, text)) in got.iter().zip(&model) {
                prop_assert_eq!(&a.text, text);
                prop_assert_eq!(a.level == "warn", *warn);
            }
        }
    }

    // ── Intercom HTTP contract (in-process) ─────────────────────────

    #[tokio::test]
    async fn intercom_post_then_list_roundtrip() {
        let cfg = cfg_with("", None, "");
        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({ "text": "  Team-Meet um 10  " })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::CREATED);
        let posted = body_json(resp).await;
        assert_eq!(posted["message"]["text"], "Team-Meet um 10");

        let resp = respond(cfg, "GET", "/api/intercom", None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let listed = body_json(resp).await;
        assert_eq!(listed["messages"][0]["text"], "Team-Meet um 10");
        assert!(listed["messages"][0]["id"].is_u64());
    }

    #[tokio::test]
    async fn intercom_rejects_blank_and_oversized_text() {
        let cfg = cfg_with("", None, "");
        for text in ["", "    "] {
            let resp = respond(
                cfg.clone(),
                "POST",
                "/api/intercom",
                Some(serde_json::json!({ "text": text })),
            )
            .await;
            assert_eq!(resp.status(), StatusCode::BAD_REQUEST, "text={text:?}");
        }
        let long = "x".repeat(intercom::MAX_TEXT + 1);
        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({ "text": long })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        // nothing leaked into the store
        let listed = body_json(respond(cfg, "GET", "/api/intercom", None).await).await;
        assert_eq!(listed["messages"].as_array().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn intercom_attachment_foreign_host_rejected_without_store_write() {
        let cfg = cfg_with("", None, "");
        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({
                "text": "see attached",
                "attachment_url": "https://evil.example/s/abc"
            })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let err = body_json(resp).await;
        assert!(err["error"].as_str().unwrap_or("").contains("not allowed"));
        let listed = body_json(respond(cfg, "GET", "/api/intercom", None).await).await;
        assert_eq!(listed["messages"].as_array().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn intercom_attachment_unreachable_upstream_is_400() {
        let cfg = cfg_with("", None, "");
        // allowlisted host but nothing answers there — the metadata HEAD
        // fails and the message is rejected (no silent text-only post).
        let resp = respond(
            cfg,
            "POST",
            "/api/intercom",
            Some(serde_json::json!({
                "text": "see attached",
                "attachment_url": "https://cloud.example/s/abc"
            })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    // ── Prometheus /metrics ─────────────────────────────────────────

    async fn metrics_body() -> String {
        let resp = respond(cfg_with("", None, ""), "GET", "/metrics", None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        String::from_utf8(bytes.to_vec()).expect("metrics body is UTF-8")
    }

    fn metric_value(body: &str, name: &str) -> u64 {
        body.lines()
            .find_map(|line| {
                let rest = line.strip_prefix(name)?.strip_prefix(' ')?;
                rest.parse().ok()
            })
            .expect("metric line present")
    }

    #[tokio::test]
    async fn metrics_endpoint_exposes_all_families() {
        // The track_metrics middleware counts a request only AFTER the handler
        // ran (counter increment happens post-next.run), so a first /metrics
        // scrape never contains itself. Make a normal request first (counted),
        // scrape once, then scrape again: the second body carries both the
        // /health hit and the /metrics scrape itself.
        let _ = respond(cfg_with("", None, ""), "GET", "/health", None).await;
        let _ = metrics_body().await; // scrape #1 (counted by scrape #2)
        let body = metrics_body().await; // scrape #2: full picture
        for name in [
            "http_requests_total",
            "http_request_duration_seconds",
            "active_sessions",
            "intercom_messages_total",
            "sso_logins_total",
            "sso_login_failures_total",
        ] {
            assert!(body.contains(name), "{name} missing from /metrics");
        }
        // an ordinary request and the /metrics scrape are both observed
        // (middleware is active on both routes)
        assert!(
            body.contains("http_requests_total{method=\"GET\",route=\"/health\",status=\"200\"}")
        );
        assert!(
            body.contains("http_requests_total{method=\"GET\",route=\"/metrics\",status=\"200\"}")
        );
    }

    #[tokio::test]
    async fn metrics_observe_regular_requests_too() {
        let resp = respond(cfg_with("", None, ""), "GET", "/health", None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let body = metrics_body().await;
        assert!(
            body.contains("http_requests_total{method=\"GET\",route=\"/health\",status=\"200\"}")
        );
    }

    #[tokio::test]
    async fn intercom_post_increments_intercom_counter() {
        let before = metric_value(&metrics_body().await, "intercom_messages_total");
        let resp = respond(
            cfg_with("", None, ""),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({ "text": "count me" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::CREATED);
        let after = metric_value(&metrics_body().await, "intercom_messages_total");
        assert!(
            after > before,
            "intercom counter should increase: {before} -> {after}"
        );
    }
}
