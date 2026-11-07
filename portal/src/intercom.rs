//! Portal intercom — internal short messages with attachments.
//!
//! Epics T/U (docs/E2E-JOURNEYS.md): users post short notes; a note may
//! reference one attachment either (a) FROM the cloud service by URL —
//! the portal snapshots the attachment's METADATA at send time (HEAD
//! request) and never proxies file bodies — or (b) uploaded directly
//! from the workstation via the file picker (small files, stored next to
//! the note store; conversational ephemera, not business data).
//!
//! Store: capped FIFO. When INTERCOM_DATA_PATH is set the store persists
//! write-through to that JSON file (atomic tmp+rename) and reloads on
//! start; without it the store stays in-memory (dev/test default).

use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::hash::{BuildHasher, Hasher};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::RwLock;

pub const MAX_TEXT: usize = 2000;
pub const MAX_URL: usize = 2048;
pub const MAX_MESSAGES: usize = 50;
pub const MAX_UPLOAD: u64 = 5 * 1024 * 1024;

/// Env knob for the FIFO cap (10..=500, default MAX_MESSAGES).
pub const ENV_MAX_MESSAGES: &str = "INTERCOM_MAX_MESSAGES";

/// Message kinds. "note" is the plain team note; "info"/"warn" turn the
/// message into an operator broadcast banner (announcement styling).
pub const KIND_NOTE: &str = "note";
pub const KIND_INFO: &str = "info";
pub const KIND_WARN: &str = "warn";

/// Serde default so legacy intercom.json files (no `kind` key) load as
/// plain notes — backward compatible with pre-kind stores.
fn default_kind() -> String {
    KIND_NOTE.to_string()
}

/// Validate a requested message kind. None/empty defaults to "note";
/// anything beyond note|info|warn is rejected (HTTP 400 at the handler).
pub fn validate_kind(raw: Option<&str>) -> Result<String, String> {
    let kind = raw.unwrap_or(KIND_NOTE).trim();
    match kind {
        "" => Ok(KIND_NOTE.to_string()),
        KIND_NOTE | KIND_INFO | KIND_WARN => Ok(kind.to_string()),
        _ => Err("kind must be one of note, info, warn".to_string()),
    }
}

/// Attachment reference snapshot taken when the message was sent.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Attachment {
    pub url: String,
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    pub content_type: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct IntercomMessage {
    pub id: u64,
    pub text: String,
    /// Login display name of the sender — None only for pre-author
    /// persisted stores (serde default keeps old JSON loadable).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub author: Option<String>,
    /// Severity of the note: "note" (plain), "info"/"warn" (banner
    /// mode). Serde default keeps legacy JSON loadable as "note"; the
    /// field is always serialized so persisted JSON stays stable.
    #[serde(default = "default_kind")]
    pub kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attachment: Option<Attachment>,
    pub created_at: String,
}

#[derive(Clone, Debug)]
pub struct IntercomConfig {
    /// Hosts (or host:port) the portal accepts attachment URLs from.
    pub allowed_hosts: Vec<String>,
    /// Dev escape hatch: accept http:// attachment URLs (never in prod).
    pub allow_http: bool,
}

impl IntercomConfig {
    pub fn from_env(opensme_domain: &str) -> Self {
        // The cloud host is wherever OPENCLOUD_URL points (it differs per
        // deployment); also keep the classic cloud.<opensme-domain> name.
        let mut hosts = vec![format!("cloud.{}", opensme_domain)];
        // SOGo6 webmail (WebDAV/cloud storage): intercom notes link files
        // hosted there, e.g. https://webmail.<domain>/SOGo/dav/…
        let webmail = format!("webmail.{}", opensme_domain);
        if !hosts.contains(&webmail) {
            hosts.push(webmail);
        }
        if let Ok(oc) = std::env::var("OPENCLOUD_URL") {
            let host = derived_host_from_url(&oc);
            if !host.is_empty() && !hosts.contains(&host) {
                hosts.push(host);
            }
        }
        // SOGO6_DOMAIN (e.g. https://webmail.home.opensme.graphwiz.ai or
        // a bare host): its trimmed lowercase host joins the defaults so
        // a SOGo6 instance off the main domain stays linkable.
        if let Ok(sogo) = std::env::var("SOGO6_DOMAIN") {
            let host = derived_host_from_url(&sogo);
            if !host.is_empty() && !hosts.contains(&host) {
                hosts.push(host);
            }
        }
        let env_hosts = std::env::var("INTERCOM_ATTACHMENT_HOSTS")
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty());
        let allowed_hosts = match env_hosts {
            Some(raw) => raw
                .split(',')
                .map(|h| h.trim().to_lowercase())
                .filter(|h| !h.is_empty())
                .collect::<Vec<_>>(),
            // No env override: accept every derived default host.
            None => hosts.iter().map(|h| h.to_lowercase()).collect(),
        };
        Self {
            allowed_hosts,
            allow_http: std::env::var("INTERCOM_ALLOW_HTTP")
                .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
                .unwrap_or(false),
        }
    }
}

pub struct IntercomStore {
    messages: RwLock<VecDeque<IntercomMessage>>,
    next_id: AtomicU64,
    /// FIFO cap — INTERCOM_MAX_MESSAGES, clamped 10..=500.
    max_messages: usize,
    /// JSON persistence file; None = in-memory only.
    data_path: Option<PathBuf>,
    /// Directory for picked-file uploads; None = uploads disabled.
    attachments_dir: Option<PathBuf>,
}

impl Default for IntercomStore {
    fn default() -> Self {
        Self {
            messages: RwLock::new(VecDeque::new()),
            next_id: AtomicU64::new(1),
            max_messages: MAX_MESSAGES,
            data_path: None,
            attachments_dir: None,
        }
    }
}

/// Host (or host:port) from a URL-ish env value: trims whitespace,
/// strips an optional http(s):// scheme and any path/query, lowercases.
/// Shared by the OPENCLOUD_URL and SOGO6_DOMAIN derivations.
fn derived_host_from_url(raw: &str) -> String {
    raw.trim()
        .trim_start_matches("https://")
        .trim_start_matches("http://")
        .split('/')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase()
}

impl IntercomStore {
    /// Build a store that persists to `data_path` and stores picked-file
    /// uploads under `attachments_dir`. Loads existing messages (missing
    /// file = fresh store; corrupt file = rename aside, start fresh).
    pub fn open(data_path: Option<PathBuf>, attachments_dir: Option<PathBuf>) -> Self {
        let max_messages = std::env::var(ENV_MAX_MESSAGES)
            .ok()
            .and_then(|v| v.trim().parse::<usize>().ok())
            .map(|n| n.clamp(10, 500))
            .unwrap_or(MAX_MESSAGES);
        let store = Self {
            messages: RwLock::new(VecDeque::new()),
            next_id: AtomicU64::new(1),
            max_messages,
            data_path,
            attachments_dir,
        };
        store.load();
        store
    }

    fn load(&self) {
        let Some(path) = &self.data_path else { return };
        let Ok(raw) = std::fs::read(path) else { return };
        match serde_json::from_slice::<Vec<IntercomMessage>>(&raw) {
            Ok(msgs) => {
                let mut messages = self.messages.write().unwrap_or_else(|e| e.into_inner());
                let next = msgs.iter().map(|m| m.id).max().unwrap_or(0) + 1;
                self.next_id.store(next, Ordering::Relaxed);
                // The snapshot file is oldest-first (persist walks the
                // deque front→back) — load it in the same order so
                // newest-first list()/list_since(), latest_id() (back)
                // and the FIFO front (oldest) all stay correct.
                *messages = msgs.into_iter().collect();
            }
            Err(_) => {
                // Corrupt store: keep it for forensics, start fresh.
                let _ = std::fs::rename(path, path.with_extension("json.corrupt"));
            }
        }
    }

    /// Atomic write-through: serialize then rename over the target.
    fn persist(&self, messages: &[IntercomMessage]) {
        let Some(path) = &self.data_path else { return };
        let Ok(json) = serde_json::to_vec(messages) else {
            return;
        };
        let tmp = path.with_extension("json.tmp");
        if std::fs::write(&tmp, json).is_ok() {
            let _ = std::fs::rename(&tmp, path);
        }
    }

    /// Store an uploaded file. Returns the sanitized name and a random
    /// token; the file lives at attachments_dir/<token>_<name>.
    pub fn save_attachment(
        &self,
        file_name: &str,
        bytes: &[u8],
    ) -> Result<(String, String), String> {
        let dir = self
            .attachments_dir
            .as_ref()
            .ok_or_else(|| "attachment uploads are disabled".to_string())?;
        if bytes.is_empty() {
            return Err("attachment is empty".into());
        }
        if bytes.len() as u64 > MAX_UPLOAD {
            return Err(format!("attachment exceeds {} bytes", MAX_UPLOAD));
        }
        let name = sanitize_name(file_name);
        let token = random_token();
        let target = dir.join(format!("{}_{}", token, name));
        std::fs::write(&target, bytes).map_err(|e| format!("cannot store attachment: {}", e))?;
        Ok((token, name))
    }

    /// Read an attachment by token+name. Refuses anything but the exact
    /// <hex-token>_<sanitized-name> layout — no traversal, no globs.
    pub fn read_attachment(&self, token: &str, name: &str) -> Option<(Vec<u8>, String)> {
        let dir = self.attachments_dir.as_ref()?;
        if token.is_empty() || !token.bytes().all(|b| b.is_ascii_hexdigit()) {
            return None;
        }
        let safe = sanitize_name(name);
        if safe != name {
            return None;
        }
        let path = dir.join(format!("{}_{}", token, safe));
        std::fs::read(&path).ok().map(|bytes| {
            let ct = mime_for(&safe);
            (bytes, ct)
        })
    }

    fn push_internal(
        &self,
        text: String,
        author: Option<String>,
        attachment: Option<Attachment>,
        kind: String,
    ) -> IntercomMessage {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        // Unix seconds — enough for ordering; no extra time crate needed.
        let created_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs().to_string())
            .unwrap_or_default();
        IntercomMessage {
            id,
            text,
            author,
            kind,
            attachment,
            created_at,
        }
    }

    pub fn push(
        &self,
        text: String,
        author: Option<String>,
        attachment: Option<Attachment>,
        kind: String,
    ) -> IntercomMessage {
        let msg = self.push_internal(text, author, attachment, kind);
        // Single write-lock for mutate + snapshot: no second acquisition,
        // and the persisted snapshot can never race another push.
        let mut messages = self.messages.write().unwrap_or_else(|e| e.into_inner());
        messages.push_back(msg.clone());
        // Rotated-out messages: their picked-file uploads die with the
        // note (cloud links survive — the file stays in the cloud).
        let mut dropped = Vec::new();
        while messages.len() > self.max_messages {
            if let Some(old) = messages.pop_front() {
                dropped.push(old);
            }
        }
        let snapshot: Vec<IntercomMessage> = messages.iter().cloned().collect();
        drop(messages);
        self.persist(&snapshot);
        for old in dropped {
            self.gc_local_attachment(&old);
        }
        msg
    }

    /// Best-effort delete of a rotated-out message's picked-file upload:
    /// only exact /api/intercom/files/<token>/<name> URLs, laid out as
    /// attachments_dir/<token>_<name>. Never touches anything else.
    fn gc_local_attachment(&self, msg: &IntercomMessage) {
        if let Some(dir) = self.attachments_dir.as_ref() {
            remove_local_attachment(dir, &msg.attachment, "/api/intercom/files/");
        }
    }

    /// Moderation: remove one note by id. Persists an updated snapshot
    /// (same atomic tmp+rename as push) and best-effort GCs the note's
    /// picked-file upload — ONE code path with the FIFO rotation
    /// (gc_local_attachment → remove_local_attachment); cloud links
    /// survive (the file stays in the cloud). Returns false — and
    /// touches nothing, no persist — when the id is unknown.
    pub fn delete(&self, id: u64) -> bool {
        let mut messages = self.messages.write().unwrap_or_else(|e| e.into_inner());
        let before = messages.len();
        let mut removed: Option<IntercomMessage> = None;
        messages.retain(|m| {
            if removed.is_none() && m.id == id {
                removed = Some(m.clone());
                false
            } else {
                true
            }
        });
        if messages.len() == before {
            return false;
        }
        let snapshot: Vec<IntercomMessage> = messages.iter().cloned().collect();
        drop(messages);
        self.persist(&snapshot);
        if let Some(msg) = removed {
            self.gc_local_attachment(&msg);
        }
        true
    }

    pub fn list(&self) -> Vec<IntercomMessage> {
        self.messages
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .rev()
            .cloned()
            .collect()
    }

    /// Delta listing for polling clients: newest-first messages with
    /// id > `since` (since = 0 behaves like a full list()).
    pub fn list_since(&self, since: u64) -> Vec<IntercomMessage> {
        self.messages
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .rev()
            .filter(|m| m.id > since)
            .cloned()
            .collect()
    }

    /// Highest id currently held (0 on an empty store) — lets polling
    /// clients initialize their cursor.
    pub fn latest_id(&self) -> u64 {
        self.messages
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .back()
            .map(|m| m.id)
            .unwrap_or(0)
    }
}

/// Filesystem-safe attachment name: keep letters/digits/dot/dash/underscore,
/// collapse everything else to '_', cap length, never empty, never dotfile.
pub(crate) fn sanitize_name(raw: &str) -> String {
    let cleaned: String = raw
        .chars()
        .take(120)
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_') {
                c
            } else {
                '_'
            }
        })
        .collect();
    let cleaned = cleaned.trim_matches('.').trim_end_matches('_').to_string();
    if cleaned.is_empty() || cleaned == "_" {
        "file".into()
    } else {
        cleaned
    }
}

/// 16 hex chars from the OS hasher's random keys — no extra crate.
pub(crate) fn random_token() -> String {
    let mut h = std::collections::hash_map::RandomState::new().build_hasher();
    h.write_u64(0);
    let a = h.finish();
    let mut h = std::collections::hash_map::RandomState::new().build_hasher();
    h.write_u64(0);
    format!("{:016x}{:016x}", a, h.finish())
}

/// Best-effort removal of a rotated-out entity's LOCAL upload (shared by
/// the intercom and polls stores). Only exact `<prefix><token>/<name>`
/// URLs are honored — token must be hex, name must already be sanitized —
/// so nothing outside attachments_dir can ever be touched.
pub(crate) fn remove_local_attachment(
    dir: &std::path::Path,
    attachment: &Option<Attachment>,
    prefix: &str,
) {
    let Some(att) = attachment.as_ref() else {
        return;
    };
    let Some(rest) = att.url.strip_prefix(prefix) else {
        return;
    };
    let Some((token, name)) = rest.split_once('/') else {
        return;
    };
    if token.is_empty()
        || !token.bytes().all(|b| b.is_ascii_hexdigit())
        || sanitize_name(name) != name
    {
        return;
    }
    let _ = std::fs::remove_file(dir.join(format!("{token}_{name}")));
}

/// Minimal content-type mapping for serving picked files inline.
pub(crate) fn mime_for(name: &str) -> String {
    let ext = name.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
    match ext.as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "svg" => "image/svg+xml",
        "pdf" => "application/pdf",
        "txt" | "md" => "text/plain; charset=utf-8",
        "json" => "application/json",
        "csv" => "text/csv",
        _ => "application/octet-stream",
    }
    .to_string()
}

/// Active-content extensions are served with `Content-Disposition:
/// attachment` — never inline. An inline `image/svg+xml` (or html/xml)
/// from the own origin executes attacker JS in the PORTAL's origin and
/// can act on the victim's session (stored XSS via picked file).
pub(crate) fn serve_inline(name: &str) -> bool {
    let ext = name.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
    !matches!(
        ext.as_str(),
        "svg" | "svgz" | "html" | "htm" | "xhtml" | "xml" | "swf"
    )
}

// ── Validation ────────────────────────────────────────────────────────────

pub fn validate_text(raw: &str) -> Result<String, String> {
    let text = raw.trim();
    if text.is_empty() {
        return Err("message text is empty".into());
    }
    if text.len() > MAX_TEXT {
        return Err(format!("message text exceeds {} bytes", MAX_TEXT));
    }
    Ok(text.to_string())
}

pub fn validate_attachment_url(raw: &str, cfg: &IntercomConfig) -> Result<String, String> {
    if raw.len() > MAX_URL {
        return Err(format!("attachment url exceeds {} bytes", MAX_URL));
    }
    let url =
        reqwest::Url::parse(raw).map_err(|_| "attachment url is not a valid URL".to_string())?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err("attachment url must not carry credentials".into());
    }
    let scheme = url.scheme();
    if scheme != "https" && !(scheme == "http" && cfg.allow_http) {
        return Err("attachment url must use https".into());
    }
    let host = url
        .host_str()
        .ok_or_else(|| "attachment url has no host".to_string())?;
    // IP literals are never accepted — the cloud is addressed by name.
    if host.parse::<std::net::IpAddr>().is_ok() || host.starts_with('[') {
        return Err("attachment url must address the cloud by name, not IP".into());
    }
    let host_lower = host.to_lowercase();
    let host_with_port = match url.port() {
        Some(p) => format!("{}:{}", host_lower, p),
        None => host_lower.clone(),
    };
    let allowed = cfg
        .allowed_hosts
        .iter()
        .any(|h| *h == host_with_port || (*h == host_lower && url.port().is_none()));
    if !allowed {
        return Err(format!("attachment host not allowed: {}", host));
    }
    Ok(url.to_string())
}

pub fn parse_attachment_metadata(
    url: &str,
    content_type: Option<&str>,
    content_length: Option<u64>,
    content_disposition: Option<&str>,
) -> Attachment {
    let name = content_disposition
        .and_then(|cd| {
            cd.split(';')
                .map(|p| p.trim())
                .find_map(|p| {
                    p.strip_prefix("filename=")
                        .or_else(|| p.strip_prefix("filename*="))
                })
                .map(|f| {
                    f.trim()
                        .trim_matches('"')
                        .rsplit("''") // RFC 5987 filename*=UTF-8''name
                        .next()
                        .unwrap_or(f)
                        .to_string()
                })
        })
        .filter(|f| !f.is_empty())
        .unwrap_or_else(|| {
            reqwest::Url::parse(url)
                .ok()
                .and_then(|u| {
                    u.path_segments()
                        .and_then(|mut segs| segs.next_back())
                        .map(|s| s.to_string())
                })
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "attachment".into())
        });
    Attachment {
        url: url.to_string(),
        name,
        size: content_length,
        content_type: content_type
            .unwrap_or("application/octet-stream")
            .split(';')
            .next()
            .unwrap_or("application/octet-stream")
            .trim()
            .to_string(),
    }
}

// ── store impl lives above (persistence + attachments) ──

#[cfg(test)]
mod tests {
    // ── attachment hosts (derived defaults: cloud + webmail + SOGo6) ──

    /// Env-mutating tests must not race each other: cargo runs them in
    /// parallel threads inside one process. Everything that touches
    /// process env takes this lock for its whole body.
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    #[test]
    fn default_hosts_cover_opencloud_url_host_even_with_empty_env() {
        // Regression: with the composed default being a comma-joined
        // string, an empty-but-set INTERCOM_ATTACHMENT_HOSTS produced a
        // single allowlist entry containing a comma — nothing matched.
        // SAFETY: single-threaded env access in this test binary section.
        let _env = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        unsafe {
            std::env::set_var("OPENCLOUD_URL", "http://cloud.opensme.local");
            std::env::set_var("INTERCOM_ATTACHMENT_HOSTS", "");
        }
        let cfg = IntercomConfig::from_env("meine-firma.test");
        assert!(cfg
            .allowed_hosts
            .contains(&"cloud.meine-firma.test".to_string()));
        assert!(cfg
            .allowed_hosts
            .contains(&"cloud.opensme.local".to_string()));
        let ok = validate_attachment_url("https://cloud.meine-firma.test/Angebot_2026.pdf", &cfg);
        assert!(ok.is_ok());
        unsafe {
            std::env::remove_var("OPENCLOUD_URL");
            std::env::remove_var("INTERCOM_ATTACHMENT_HOSTS");
        }
    }

    #[test]
    fn webmail_subdomain_is_allowlisted_by_default() {
        // SOGo6 webmail / WebDAV: notes may link files hosted on
        // webmail.<opensme-domain> even with no env hints at all.
        let _env = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        unsafe {
            std::env::remove_var("OPENCLOUD_URL");
            std::env::remove_var("INTERCOM_ATTACHMENT_HOSTS");
            std::env::remove_var("SOGO6_DOMAIN");
        }
        let cfg = IntercomConfig::from_env("meine-firma.test");
        assert!(cfg
            .allowed_hosts
            .contains(&"webmail.meine-firma.test".to_string()));
        assert!(validate_attachment_url(
            "https://webmail.meine-firma.test/SOGo/dav/freigabe.pdf",
            &cfg
        )
        .is_ok());
        unsafe {
            std::env::remove_var("SOGO6_DOMAIN");
        }
    }

    #[test]
    fn sogo6_domain_env_adds_its_host_to_the_allowlist() {
        let _env = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        unsafe {
            std::env::remove_var("OPENCLOUD_URL");
            std::env::remove_var("INTERCOM_ATTACHMENT_HOSTS");
            // Scheme + path are stripped; only the host is allowlisted.
            std::env::set_var("SOGO6_DOMAIN", "https://Webmail.home.opensme.graphwiz.ai/");
        }
        let cfg = IntercomConfig::from_env("meine-firma.test");
        assert!(cfg
            .allowed_hosts
            .contains(&"webmail.home.opensme.graphwiz.ai".to_string()));
        assert!(validate_attachment_url(
            "https://webmail.home.opensme.graphwiz.ai/SOGo/dav/notizen/x.pdf",
            &cfg
        )
        .is_ok());
        // A different host is still rejected.
        assert!(validate_attachment_url("https://evil.example/x.pdf", &cfg).is_err());
        unsafe {
            std::env::remove_var("SOGO6_DOMAIN");
        }
    }

    #[test]
    fn intercom_attachment_hosts_env_still_overrides_everything() {
        let _env = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        unsafe {
            std::env::remove_var("OPENCLOUD_URL");
            std::env::set_var("SOGO6_DOMAIN", "https://webmail.home.opensme.graphwiz.ai");
            std::env::set_var("INTERCOM_ATTACHMENT_HOSTS", "files.example");
        }
        let cfg = IntercomConfig::from_env("meine-firma.test");
        assert_eq!(cfg.allowed_hosts, vec!["files.example".to_string()]);
        assert!(validate_attachment_url("https://files.example/x.pdf", &cfg).is_ok());
        // Derived hosts (cloud/webmail/SOGO6) are NOT accepted anymore.
        assert!(validate_attachment_url("https://webmail.meine-firma.test/x", &cfg).is_err());
        assert!(
            validate_attachment_url("https://webmail.home.opensme.graphwiz.ai/x", &cfg).is_err()
        );
        unsafe {
            std::env::remove_var("SOGO6_DOMAIN");
            std::env::remove_var("INTERCOM_ATTACHMENT_HOSTS");
        }
    }

    use super::*;

    fn cfg() -> IntercomConfig {
        IntercomConfig {
            allowed_hosts: vec!["cloud.example".into()],
            allow_http: false,
        }
    }

    // ── text ──────────────────────────────────────────────────────────

    #[test]
    fn text_is_trimmed_and_kept() {
        assert_eq!(validate_text("  Hallo Team  ").unwrap(), "Hallo Team");
    }

    #[test]
    fn text_blank_rejected() {
        assert!(validate_text("").is_err());
        assert!(validate_text("   \n\t ").is_err());
    }

    #[test]
    fn text_over_limit_rejected() {
        let long = "x".repeat(MAX_TEXT + 1);
        assert!(validate_text(&long).is_err());
        assert!(validate_text(&"x".repeat(MAX_TEXT)).is_ok());
    }

    // ── attachment URLs ───────────────────────────────────────────────

    #[test]
    fn attachment_url_https_allowlisted_ok() {
        let url = validate_attachment_url("https://cloud.example/s/abc123", &cfg()).unwrap();
        assert_eq!(url, "https://cloud.example/s/abc123");
    }

    #[test]
    fn attachment_url_foreign_host_rejected() {
        assert!(validate_attachment_url("https://evil.example/s/abc", &cfg()).is_err());
    }

    #[test]
    fn attachment_url_http_rejected_unless_allowed() {
        assert!(validate_attachment_url("http://cloud.example/s/abc", &cfg()).is_err());
        let dev = IntercomConfig {
            allowed_hosts: vec!["cloud.example".into()],
            allow_http: true,
        };
        assert!(validate_attachment_url("http://cloud.example/s/abc", &dev).is_ok());
    }

    #[test]
    fn attachment_url_allowlist_host_port_matching() {
        let dev = IntercomConfig {
            allowed_hosts: vec!["gateway.local:8099".into()],
            allow_http: true,
        };
        assert!(validate_attachment_url("http://gateway.local:8099/files/x.pdf", &dev).is_ok());
        // same host, wrong port → not on the allowlist
        assert!(validate_attachment_url("http://gateway.local:1234/files/x.pdf", &dev).is_err());
    }

    #[test]
    fn attachment_url_ip_literals_always_rejected() {
        let listed = IntercomConfig {
            allowed_hosts: vec!["127.0.0.1".into()],
            allow_http: true,
        };
        assert!(validate_attachment_url("https://127.0.0.1/s/abc", &listed).is_err());
        let v6 = "https://[::1]/s/abc";
        assert!(validate_attachment_url(v6, &cfg()).is_err());
    }

    #[test]
    fn attachment_url_userinfo_rejected() {
        assert!(validate_attachment_url("https://evil@cloud.example/s/abc", &cfg()).is_err());
    }

    #[test]
    fn attachment_url_size_capped() {
        let long = format!("https://cloud.example/s/{}", "a".repeat(MAX_URL));
        assert!(validate_attachment_url(&long, &cfg()).is_err());
    }

    // ── metadata snapshot ─────────────────────────────────────────────

    #[test]
    fn metadata_from_disposition_headers() {
        let a = parse_attachment_metadata(
            "https://cloud.example/s/abc",
            Some("application/pdf"),
            Some(12345),
            Some("attachment; filename=\"Rechnung 2026.pdf\""),
        );
        assert_eq!(a.name, "Rechnung 2026.pdf");
        assert_eq!(a.size, Some(12345));
        assert_eq!(a.content_type, "application/pdf");
    }

    #[test]
    fn metadata_falls_back_to_last_path_segment() {
        let a = parse_attachment_metadata(
            "https://cloud.example/s/files/Bericht.docx",
            None,
            None,
            None,
        );
        assert_eq!(a.name, "Bericht.docx");
        assert_eq!(a.size, None);
        assert_eq!(a.content_type, "application/octet-stream");
    }

    // ── store ─────────────────────────────────────────────────────────

    #[test]
    fn store_caps_fifo_and_lists_newest_first() {
        let store = IntercomStore::default();
        for i in 0..(MAX_MESSAGES + 10) {
            store.push(format!("m{i}"), None, None, KIND_NOTE.into());
        }
        let listed = store.list();
        assert_eq!(listed.len(), MAX_MESSAGES);
        assert_eq!(listed[0].text, format!("m{}", MAX_MESSAGES + 9));
        assert!(!listed.iter().any(|m| m.text == "m0"));
    }

    #[test]
    fn store_message_carries_attachment_snapshot() {
        let store = IntercomStore::default();
        let att = parse_attachment_metadata(
            "https://cloud.example/s/abc",
            Some("application/pdf"),
            Some(7),
            Some("attachment; filename=\"a.pdf\""),
        );
        let m = store.push(
            "see attached".into(),
            Some("Bob".into()),
            Some(att.clone()),
            KIND_NOTE.into(),
        );
        let stored = &store.list()[0];
        assert_eq!(stored.attachment.as_ref().unwrap().name, "a.pdf");
        assert_eq!(stored.author.as_deref(), Some("Bob"));
        assert_eq!(m.id, 1);
    }

    // ── persistence ─────────────────────────────────────────────────

    #[test]
    fn store_survives_reopen_from_disk() {
        let dir = std::env::temp_dir().join(format!("ic-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("intercom.json");
        let _ = std::fs::remove_file(&path);

        let store = IntercomStore::open(Some(path.clone()), None);
        store.push(
            "persistent".into(),
            Some("Anna".into()),
            None,
            KIND_NOTE.into(),
        );
        drop(store);

        let store2 = IntercomStore::open(Some(path.clone()), None);
        let listed = store2.list();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].text, "persistent");
        // id counter continues after reload
        assert_eq!(
            store2
                .push("second".into(), None, None, KIND_NOTE.into())
                .id,
            listed[0].id + 1
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn store_fifo_cap_survives_reload() {
        let dir = std::env::temp_dir().join(format!("ic-test-cap-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("intercom.json");
        let _ = std::fs::remove_file(&path);

        let store = IntercomStore::open(Some(path.clone()), None);
        for i in 0..(MAX_MESSAGES + 5) {
            store.push(format!("m{i}"), None, None, KIND_NOTE.into());
        }
        drop(store);
        let store2 = IntercomStore::open(Some(path), None);
        let listed = store2.list();
        assert_eq!(listed.len(), MAX_MESSAGES);
        assert!(!listed.iter().any(|m| m.text == "m0"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn store_corrupt_file_renamed_aside() {
        let dir = std::env::temp_dir().join(format!("ic-test-corrupt-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("intercom.json");
        std::fs::write(&path, b"{not json").unwrap();

        let store = IntercomStore::open(Some(path.clone()), None);
        assert!(store.list().is_empty());
        assert!(path.with_extension("json.corrupt").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── author + delta listing ───────────────────────────────────────

    #[test]
    fn author_stamped_and_persisted() {
        let dir = std::env::temp_dir().join(format!("ic-test-auth-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("intercom.json");
        let _ = std::fs::remove_file(&path);

        let store = IntercomStore::open(Some(path.clone()), None);
        store.push(
            "hallo".into(),
            Some("Tobias".into()),
            None,
            KIND_NOTE.into(),
        );
        drop(store);

        let re = IntercomStore::open(Some(path.clone()), None);
        assert_eq!(re.list()[0].author.as_deref(), Some("Tobias"));

        // Pre-author JSON (no author key) still loads: author = None.
        std::fs::write(&path, r#"[{"id":1,"text":"legacy","created_at":"1"}]"#).unwrap();
        let legacy = IntercomStore::open(Some(path), None);
        assert_eq!(legacy.list()[0].author, None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn list_since_is_delta_and_latest_id_tracks() {
        let store = IntercomStore::default();
        assert_eq!(store.latest_id(), 0);
        for i in 0..5 {
            store.push(format!("m{i}"), None, None, KIND_NOTE.into());
        }
        assert_eq!(store.latest_id(), 5);
        let delta = store.list_since(3);
        assert_eq!(delta.iter().map(|m| m.id).collect::<Vec<_>>(), vec![5, 4]);
        assert!(store.list_since(5).is_empty());
        // since=0 = full list
        assert_eq!(store.list_since(0).len(), store.list().len());
    }

    #[test]
    fn rotated_upload_files_are_gc_collected() {
        let dir = std::env::temp_dir().join(format!(
            "ic-test-gc-{}-{}",
            std::process::id(),
            random_token()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        // Push MAX+2 notes each with an upload; the first notes rotate out
        // and their upload files must be gc-collected, later ones stay.
        let data = dir.join("data.json");
        let atts = dir.join("attachments");
        std::fs::create_dir_all(&atts).unwrap();
        let store = IntercomStore::open(Some(data), Some(atts.clone()));
        let mut first_url = None;
        for i in 0..(MAX_MESSAGES + 2) {
            let bytes = format!("body-{i}").into_bytes();
            let (token, name) = store.save_attachment(&format!("f{i}.txt"), &bytes).unwrap();
            let att = Attachment {
                url: format!("/api/intercom/files/{token}/{name}"),
                name: name.clone(),
                size: Some(bytes.len() as u64),
                content_type: "text/plain".into(),
            };
            if i == 0 {
                first_url = Some(att.url.clone());
            }
            store.push(format!("m{i}"), None, Some(att), KIND_NOTE.into());
        }
        // The first message rotated out — its upload file must be gone.
        let remaining = std::fs::read_dir(&atts).unwrap().count();
        assert_eq!(remaining, MAX_MESSAGES, "rotated file must be gc'd");
        let first = first_url.unwrap();
        let rest = first.strip_prefix("/api/intercom/files/").unwrap();
        let (token, name) = rest.split_once('/').unwrap();
        assert!(!atts.join(format!("{token}_{name}")).exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn serve_inline_blocks_active_content() {
        assert!(!serve_inline("evil.svg"));
        assert!(!serve_inline("evil.svgz"));
        assert!(!serve_inline("page.html"));
        assert!(!serve_inline("data.xml"));
        // no extension → rsplit gives the whole name → not blocked → inline
        assert!(serve_inline("noext"));
        assert!(serve_inline("photo.png"));
        assert!(serve_inline("report.pdf"));
        assert!(serve_inline("notes.txt"));
    }

    // ── picked-file attachments ─────────────────────────────────────

    #[test]
    fn upload_roundtrip_and_mime() {
        let dir = std::env::temp_dir().join(format!("ic-test-up-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let store = IntercomStore::open(None, Some(dir.clone()));

        let (token, name) = store
            .save_attachment("Bericht 2026.pdf", b"%PDF-1.4")
            .unwrap();
        assert!(name.ends_with(".pdf"));
        assert_eq!(token.len(), 32);
        let (bytes, ct) = store.read_attachment(&token, &name).unwrap();
        assert_eq!(bytes, b"%PDF-1.4");
        assert_eq!(ct, "application/pdf");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn upload_rejects_oversize_and_empty() {
        let dir = std::env::temp_dir().join(format!("ic-test-size-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let store = IntercomStore::open(None, Some(dir));
        assert!(store.save_attachment("a.txt", b"").is_err());
        let big = vec![0u8; (MAX_UPLOAD + 1) as usize];
        assert!(store.save_attachment("big.bin", &big).is_err());
    }

    #[test]
    fn upload_disabled_without_dir() {
        let store = IntercomStore::default();
        assert!(store.save_attachment("a.txt", b"x").is_err());
    }

    #[test]
    fn sanitize_never_allows_traversal_or_weird_names() {
        let out = sanitize_name("../../etc/passwd");
        assert!(!out.contains('/'), "no path separators: {out}");
        // join() is safe as long as there are no separators and no leading dot
        assert!(!out.starts_with('.'), "no leading dot: {out}");
        assert_eq!(sanitize_name(""), "file");
        assert_eq!(sanitize_name("..."), "file");
        assert_eq!(sanitize_name("a b/c.txt"), "a_b_c.txt");
    }

    #[test]
    fn read_attachment_refuses_non_matching_layout() {
        let dir = std::env::temp_dir().join(format!("ic-test-trav-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let store = IntercomStore::open(None, Some(dir.clone()));
        let (token, name) = store.save_attachment("a.txt", b"hi").unwrap();

        // wrong token charset
        assert!(store.read_attachment("../..", &name).is_none());
        // name with traversal chars never matches the sanitized file
        assert!(store.read_attachment(&token, "../x.txt").is_none());
        // unknown token
        assert!(store
            .read_attachment("deadbeefdeadbeefdeadbeefdeadbeef", &name)
            .is_none());
        // the real one works
        assert!(store.read_attachment(&token, &name).is_some());
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── kind / severity ──────────────────────────────────────────────

    #[test]
    fn validate_kind_accepts_only_note_info_warn() {
        assert_eq!(validate_kind(None).unwrap(), "note");
        assert_eq!(validate_kind(Some("")).unwrap(), "note");
        assert_eq!(validate_kind(Some("  note ")).unwrap(), "note");
        assert_eq!(validate_kind(Some("info")).unwrap(), "info");
        assert_eq!(validate_kind(Some("warn")).unwrap(), "warn");
        assert_eq!(
            validate_kind(Some("WARM")).unwrap_err(),
            "kind must be one of note, info, warn"
        );
        assert!(validate_kind(Some("banana")).is_err());
    }

    #[test]
    fn kind_roundtrips_through_push_and_list() {
        let store = IntercomStore::default();
        let plain = store.push("hallo".into(), None, None, validate_kind(None).unwrap());
        assert_eq!(plain.kind, "note");
        let warn = store.push(
            "wartung".into(),
            None,
            None,
            validate_kind(Some("warn")).unwrap(),
        );
        let listed = store.list();
        assert_eq!(listed[0].kind, "warn");
        assert_eq!(listed[0].id, warn.id);
        assert_eq!(listed[1].kind, "note");
        assert_eq!(listed[1].id, plain.id);
        // list_since keeps the kind too.
        let delta = store.list_since(plain.id);
        assert_eq!(delta.len(), 1);
        assert_eq!(delta[0].kind, "warn");
    }

    #[test]
    fn legacy_json_without_kind_loads_as_note() {
        let dir = std::env::temp_dir().join(format!(
            "ic-test-kind-{}-{}",
            std::process::id(),
            random_token()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("intercom.json");
        // Pre-kind JSON (author era) must still load: kind = "note".
        std::fs::write(
            &path,
            r#"[{"id":1,"text":"legacy","author":"Alt","created_at":"1"}]"#,
        )
        .unwrap();
        let store = IntercomStore::open(Some(path.clone()), None);
        assert_eq!(store.list()[0].kind, "note");

        // A kind written today survives reopen.
        drop(store);
        let store = IntercomStore::open(Some(path.clone()), None);
        store.push(
            "banner".into(),
            None,
            None,
            validate_kind(Some("info")).unwrap(),
        );
        drop(store);
        let raw = std::fs::read_to_string(&path).unwrap();
        assert!(raw.contains("\"kind\":\"info\""), "raw json: {raw}");
        let re = IntercomStore::open(Some(path), None);
        // Newest-first after reload, kind survives, cursor correct.
        let listed = re.list();
        assert_eq!(listed.len(), 2);
        assert_eq!(listed[0].kind, "info");
        assert_eq!(listed[1].kind, "note");
        assert_eq!(re.latest_id(), listed[0].id);
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── moderation delete ────────────────────────────────────────────

    #[test]
    fn delete_removes_persists_and_keeps_id_space() {
        let dir = std::env::temp_dir().join(format!(
            "ic-test-del-{}-{}",
            std::process::id(),
            random_token()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("intercom.json");

        let store = IntercomStore::open(Some(path.clone()), None);
        let a = store.push("a".into(), None, None, KIND_NOTE.into());
        let b = store.push("b".into(), None, None, KIND_NOTE.into());
        assert!(store.delete(a.id));
        let listed = store.list();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].id, b.id);
        drop(store);

        // The updated snapshot was persisted — reopen sees the delete.
        let re = IntercomStore::open(Some(path.clone()), None);
        let listed = re.list();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].text, "b");
        assert_eq!(re.latest_id(), b.id, "cursor = highest id held");
        // Deleted ids are never reused — the id counter survived.
        assert_eq!(
            re.push("c".into(), None, None, KIND_NOTE.into()).id,
            b.id + 1
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn delete_unknown_id_returns_false_and_does_not_persist() {
        let dir = std::env::temp_dir().join(format!(
            "ic-test-delmiss-{}-{}",
            std::process::id(),
            random_token()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("intercom.json");

        let store = IntercomStore::open(Some(path.clone()), None);
        let m = store.push("keep".into(), None, None, KIND_NOTE.into());
        assert!(store.list().len() == 1);
        // Sentinel on disk: a persist on the miss would overwrite it.
        std::fs::write(&path, b"[]").unwrap();
        assert!(!store.delete(m.id + 100));
        assert_eq!(store.list().len(), 1, "unknown id must touch nothing");
        drop(store);
        assert_eq!(
            std::fs::read(&path).unwrap(),
            b"[]",
            "a missed delete must not persist"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn delete_gcs_picked_file_but_leaves_cloud_attachments() {
        let dir = std::env::temp_dir().join(format!(
            "ic-test-delgc-{}-{}",
            std::process::id(),
            random_token()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let data = dir.join("data.json");
        let atts = dir.join("attachments");
        std::fs::create_dir_all(&atts).unwrap();
        let store = IntercomStore::open(Some(data), Some(atts.clone()));

        let (token, name) = store.save_attachment("lokal.txt", b"body").unwrap();
        let local = Attachment {
            url: format!("/api/intercom/files/{token}/{name}"),
            name: name.clone(),
            size: Some(4),
            content_type: "text/plain".into(),
        };
        let cloud = parse_attachment_metadata(
            "https://cloud.example/s/f.pdf",
            Some("application/pdf"),
            Some(9),
            Some("attachment; filename=\"f.pdf\""),
        );
        let m_local = store.push("lokal".into(), None, Some(local), KIND_NOTE.into());
        let m_cloud = store.push("cloud".into(), None, Some(cloud), KIND_NOTE.into());
        let picked_file = atts.join(format!("{token}_{name}"));
        assert!(picked_file.exists(), "picked file stored on push");

        // Cloud link: nothing on disk to remove — note goes, file path
        // (which never existed) is untouched, remaining note intact.
        assert!(store.delete(m_cloud.id));
        assert!(picked_file.exists());
        assert_eq!(store.list().len(), 1);

        // Picked file: gc'd together with the note.
        assert!(store.delete(m_local.id));
        assert!(!picked_file.exists(), "picked file must be gc'd on delete");
        assert!(store.list().is_empty());
        assert_eq!(std::fs::read_dir(&atts).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn fifo_cap_still_enforced_after_delete() {
        let store = IntercomStore::default();
        for i in 0..MAX_MESSAGES {
            store.push(format!("m{i}"), None, None, KIND_NOTE.into());
        }
        assert!(store.delete(1));
        assert_eq!(store.list().len(), MAX_MESSAGES - 1);
        // Subsequent pushes fill up again, then rotate — cap holds and
        // the OLDEST note (m2; m1 was already deleted) rotates out.
        store.push("fill".into(), None, None, KIND_NOTE.into());
        assert_eq!(store.list().len(), MAX_MESSAGES);
        store.push("rotate".into(), None, None, KIND_NOTE.into());
        assert_eq!(store.list().len(), MAX_MESSAGES);
        assert!(!store.list().iter().any(|m| m.text == "m1"));
        assert!(
            !store.list().iter().any(|m| m.text == "m0"),
            "oldest rotates out"
        );
        assert!(store.list().iter().any(|m| m.text == "fill"));
        assert_eq!(store.list()[0].text, "rotate");
    }
}
