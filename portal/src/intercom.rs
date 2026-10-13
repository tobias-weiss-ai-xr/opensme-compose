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
use std::sync::Mutex;

pub const MAX_TEXT: usize = 2000;
pub const MAX_URL: usize = 2048;
pub const MAX_MESSAGES: usize = 50;
pub const MAX_UPLOAD: u64 = 5 * 1024 * 1024;

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
        if let Ok(oc) = std::env::var("OPENCLOUD_URL") {
            let host = oc
                .trim()
                .trim_start_matches("https://")
                .trim_start_matches("http://")
                .split('/')
                .next()
                .unwrap_or("");
            if !host.is_empty() {
                let host = host.to_string();
                if !hosts.contains(&host) {
                    hosts.push(host);
                }
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
    messages: Mutex<VecDeque<IntercomMessage>>,
    next_id: AtomicU64,
    /// JSON persistence file; None = in-memory only.
    data_path: Option<PathBuf>,
    /// Directory for picked-file uploads; None = uploads disabled.
    attachments_dir: Option<PathBuf>,
}

impl Default for IntercomStore {
    fn default() -> Self {
        Self {
            messages: Mutex::new(VecDeque::new()),
            next_id: AtomicU64::new(1),
            data_path: None,
            attachments_dir: None,
        }
    }
}

impl IntercomStore {
    /// Build a store that persists to `data_path` and stores picked-file
    /// uploads under `attachments_dir`. Loads existing messages (missing
    /// file = fresh store; corrupt file = rename aside, start fresh).
    pub fn open(data_path: Option<PathBuf>, attachments_dir: Option<PathBuf>) -> Self {
        let store = Self {
            messages: Mutex::new(VecDeque::new()),
            next_id: AtomicU64::new(1),
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
                let mut messages = self.messages.lock().unwrap_or_else(|e| e.into_inner());
                let next = msgs.iter().map(|m| m.id).max().unwrap_or(0) + 1;
                self.next_id.store(next, Ordering::Relaxed);
                *messages = msgs.into_iter().rev().collect();
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

    fn push_internal(&self, text: String, attachment: Option<Attachment>) -> IntercomMessage {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        // Unix seconds — enough for ordering; no extra time crate needed.
        let created_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs().to_string())
            .unwrap_or_default();
        IntercomMessage {
            id,
            text,
            attachment,
            created_at,
        }
    }

    pub fn push(&self, text: String, attachment: Option<Attachment>) -> IntercomMessage {
        let msg = self.push_internal(text, attachment);
        let mut messages = self.messages.lock().unwrap_or_else(|e| e.into_inner());
        messages.push_back(msg.clone());
        while messages.len() > MAX_MESSAGES {
            messages.pop_front();
        }
        drop(messages);
        self.persist(
            &self
                .messages
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .iter()
                .cloned()
                .collect::<Vec<_>>(),
        );
        msg
    }

    pub fn list(&self) -> Vec<IntercomMessage> {
        self.messages
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .rev()
            .cloned()
            .collect()
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
    #[test]
    fn default_hosts_cover_opencloud_url_host_even_with_empty_env() {
        // Regression: with the composed default being a comma-joined
        // string, an empty-but-set INTERCOM_ATTACHMENT_HOSTS produced a
        // single allowlist entry containing a comma — nothing matched.
        // SAFETY: single-threaded env access in this test binary section.
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
            store.push(format!("m{i}"), None);
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
        let m = store.push("see attached".into(), Some(att.clone()));
        assert_eq!(store.list()[0].attachment.as_ref().unwrap().name, "a.pdf");
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
        store.push("persistent".into(), None);
        drop(store);

        let store2 = IntercomStore::open(Some(path.clone()), None);
        let listed = store2.list();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].text, "persistent");
        // id counter continues after reload
        assert_eq!(store2.push("second".into(), None).id, listed[0].id + 1);
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
            store.push(format!("m{i}"), None);
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
}
