//! Portal polls (Umfragen) — optional team feature with cloud uploads.
//!
//! Behind POLLS_ENABLED=1: without it, no card and no routes exist.
//!
//! A poll is a question with 2..=10 options; voters pick one. The store
//! persists write-through to a JSON file (same atomic tmp+rename pattern
//! as the intercom store) and is capped FIFO.
//!
//! Attachments: a poll MAY carry one file — the document being voted on.
//! Uploads go to the cloud (OpenCloud) when CLOUD_WEBDAV_URL/_USER/_PASS
//! are set: the portal PUTs the file via WebDAV and creates a public
//! share link (OCS API); the poll stores the SHARE URL, never proxying
//! downloads. Without cloud config the file falls back to the local
//! attachment dir (same <token>_<name> layout as the intercom picker).
//!
//! Vote identity: the portal has no per-user identity server-side, so
//! double-vote prevention is client-side (localStorage marker); the
//! server accepts votes from anyone who can reach the portal.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use crate::intercom::{self, Attachment};

pub const MAX_QUESTION: usize = 300;
pub const MIN_OPTIONS: usize = 2;
pub const MAX_OPTIONS: usize = 10;
pub const MAX_OPTION_LEN: usize = 120;
pub const MAX_POLLS: usize = 25;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Poll {
    pub id: u64,
    pub question: String,
    pub options: Vec<String>,
    pub votes: Vec<u32>,
    #[serde(default)]
    pub closed: bool,
    pub created_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attachment: Option<Attachment>,
}

/// OpenCloud upload credentials (WebDAV PUT + OCS public share).
#[derive(Clone, Debug)]
pub struct CloudUploadConfig {
    /// e.g. https://cloud.opensme.graphwiz.ai/remote.php/dav/files/uploader
    pub dav_base: String,
    /// OCS shares endpoint; derived from dav_base when unset.
    pub share_url: String,
    pub user: String,
    pub pass: String,
}

impl CloudUploadConfig {
    pub fn from_env() -> Option<Self> {
        let dav_base = non_empty("CLOUD_WEBDAV_URL")?;
        let user = non_empty("CLOUD_WEBDAV_USER")?;
        let pass = non_empty("CLOUD_WEBDAV_PASS")?;
        let base = dav_base_host_base(&dav_base);
        let share_url = std::env::var("CLOUD_SHARE_URL")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| format!("{base}/ocs/v2.php/apps/files_sharing/api/v1/shares"));
        Some(Self {
            dav_base: dav_base.trim_end_matches('/').to_string(),
            share_url,
            user,
            pass,
        })
    }
}

fn non_empty(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// WebDAV base host part: scheme://authority (strip everything after the
/// authority, so the OCS endpoint derives from the same host).
fn dav_base_host_base(dav_base: &str) -> String {
    let trimmed = dav_base.trim_end_matches('/');
    if let Some(rest) = trimmed.strip_prefix("https://") {
        let authority = rest.split('/').next().unwrap_or(rest);
        format!("https://{authority}")
    } else if let Some(rest) = trimmed.strip_prefix("http://") {
        let authority = rest.split('/').next().unwrap_or(rest);
        format!("http://{authority}")
    } else {
        trimmed.to_string()
    }
}

/// WebDAV file URL for an uploaded poll attachment.
pub fn dav_file_url(dav_base: &str, dir: &str, token: &str, name: &str) -> String {
    format!(
        "{}/{}_{}",
        format!("{}/{}", dav_base.trim_end_matches('/'), dir).trim_end_matches('/'),
        token,
        name
    )
}

/// Extract the public share URL from an OCS share-create response
/// (format=json → {"ocs":{"meta":{...},"data":{"url":...}}}.
pub fn parse_share_url(body: &[u8]) -> Result<String, String> {
    let v: serde_json::Value =
        serde_json::from_slice(body).map_err(|e| format!("share response not json: {e}"))?;
    let url = v["ocs"]["data"]["url"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "share response carries no url".to_string())?;
    Ok(url.to_string())
}

pub fn validate_poll(question: &str, options: &[String]) -> Result<(String, Vec<String>), String> {
    let question = question.trim();
    if question.is_empty() {
        return Err("poll question is empty".into());
    }
    if question.len() > MAX_QUESTION {
        return Err(format!("poll question exceeds {MAX_QUESTION} bytes"));
    }
    let mut opts = Vec::new();
    for o in options {
        let o = o.trim();
        if o.is_empty() {
            return Err("poll option is empty".into());
        }
        if o.len() > MAX_OPTION_LEN {
            return Err(format!("poll option exceeds {MAX_OPTION_LEN} bytes"));
        }
        opts.push(o.to_string());
    }
    if opts.len() < MIN_OPTIONS || opts.len() > MAX_OPTIONS {
        return Err(format!(
            "poll needs {MIN_OPTIONS}..{MAX_OPTIONS} options, got {}",
            opts.len()
        ));
    }
    Ok((question.to_string(), opts))
}

pub struct PollStore {
    polls: Mutex<Vec<Poll>>,
    next_id: AtomicU64,
    /// JSON persistence file; None = in-memory only.
    data_path: Option<PathBuf>,
    /// Local fallback dir for picked-file uploads; None = local uploads off.
    attachments_dir: Option<PathBuf>,
    /// Cloud (OpenCloud) upload target; None = local fallback only.
    pub cloud: Option<CloudUploadConfig>,
}

impl PollStore {
    /// Open with persistence + local-fallback dirs + optional cloud target;
    /// missing file = fresh store.
    pub fn open(
        data_path: Option<PathBuf>,
        attachments_dir: Option<PathBuf>,
        cloud: Option<CloudUploadConfig>,
    ) -> Self {
        let store = Self {
            polls: Mutex::new(Vec::new()),
            next_id: AtomicU64::new(1),
            data_path,
            attachments_dir,
            cloud,
        };
        store.load();
        store
    }

    fn load(&self) {
        let Some(path) = &self.data_path else { return };
        let Ok(raw) = std::fs::read(path) else { return };
        match serde_json::from_slice::<Vec<Poll>>(&raw) {
            Ok(polls) => {
                let next = polls.iter().map(|p| p.id).max().unwrap_or(0) + 1;
                self.next_id.store(next, Ordering::Relaxed);
                *self.polls.lock().unwrap_or_else(|e| e.into_inner()) = polls;
            }
            Err(_) => {
                let _ = std::fs::rename(path, path.with_extension("json.corrupt"));
            }
        }
    }

    fn persist(&self, polls: &[Poll]) {
        let Some(path) = &self.data_path else { return };
        let Ok(json) = serde_json::to_vec(polls) else {
            return;
        };
        let tmp = path.with_extension("json.tmp");
        if std::fs::write(&tmp, json).is_ok() {
            let _ = std::fs::rename(&tmp, path);
        }
    }

    fn snapshot(&self) -> Vec<Poll> {
        self.polls.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    pub fn list(&self) -> Vec<Poll> {
        let mut polls = self.snapshot();
        polls.reverse(); // newest first, like intercom
        polls
    }

    pub fn create(
        &self,
        question: String,
        options: Vec<String>,
        attachment: Option<Attachment>,
    ) -> Poll {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let created_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs().to_string())
            .unwrap_or_default();
        let poll = Poll {
            id,
            votes: vec![0; options.len()],
            question,
            options,
            closed: false,
            created_at,
            attachment,
        };
        let mut polls = self.polls.lock().unwrap_or_else(|e| e.into_inner());
        polls.push(poll.clone());
        while polls.len() > MAX_POLLS {
            polls.remove(0);
        }
        drop(polls);
        self.persist(&self.snapshot());
        poll
    }

    pub fn vote(&self, id: u64, option: usize) -> Result<Poll, String> {
        let result = {
            let mut polls = self.polls.lock().unwrap_or_else(|e| e.into_inner());
            let poll = polls
                .iter_mut()
                .find(|p| p.id == id)
                .ok_or("poll not found")?;
            if poll.closed {
                return Err("poll is closed".into());
            }
            if option >= poll.options.len() {
                return Err("invalid option".into());
            }
            poll.votes[option] += 1;
            Some(poll.clone())
        };
        let poll = result.expect("set above on success path");
        self.persist(&self.snapshot());
        Ok(poll)
    }

    /// Local fallback upload — same layout as the intercom file picker.
    pub fn save_attachment_local(
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
        if bytes.len() as u64 > intercom::MAX_UPLOAD {
            return Err(format!("attachment exceeds {} bytes", intercom::MAX_UPLOAD));
        }
        let name = intercom::sanitize_name(file_name);
        let token = intercom::random_token();
        std::fs::write(dir.join(format!("{token}_{name}")), bytes)
            .map_err(|e| format!("cannot store attachment: {e}"))?;
        Ok((token, name))
    }

    /// Serve a local-fallback attachment (hex token + sanitized name only).
    pub fn read_attachment_local(&self, token: &str, name: &str) -> Option<(Vec<u8>, String)> {
        let dir = self.attachments_dir.as_ref()?;
        if token.is_empty() || !token.bytes().all(|b| b.is_ascii_hexdigit()) {
            return None;
        }
        let safe = intercom::sanitize_name(name);
        if safe != name {
            return None;
        }
        std::fs::read(dir.join(format!("{token}_{safe}")))
            .ok()
            .map(|b| (b, intercom::mime_for(&safe)))
    }
}

/// Upload a poll attachment to the cloud: WebDAV PUT under
/// Umfragen/<key>/, then a public read-only share via the OCS API.
/// Returns the attachment snapshot carrying the SHARE url.
pub async fn upload_to_cloud(
    cfg: &CloudUploadConfig,
    client: &reqwest::Client,
    poll_key: &str,
    filename: &str,
    bytes: &[u8],
) -> Result<Attachment, String> {
    let name = intercom::sanitize_name(filename);
    if bytes.is_empty() {
        return Err("attachment is empty".into());
    }
    if bytes.len() as u64 > intercom::MAX_UPLOAD {
        return Err(format!("attachment exceeds {} bytes", intercom::MAX_UPLOAD));
    }
    let base = cfg.dav_base.trim_end_matches('/');
    let token = intercom::random_token();
    let rel = format!("Umfragen/{poll_key}/{token}_{name}");
    let file_url = dav_file_url(
        &cfg.dav_base,
        &format!("Umfragen/{poll_key}"),
        &token,
        &name,
    );

    // Ensure collections exist (RFC4918: 405 = already there).
    let mk = reqwest::Method::from_bytes(b"MKCOL").expect("valid method");
    for dir_url in [format!("{base}/Umfragen"), dir_url_of(base, poll_key)] {
        let _ = client
            .request(mk.clone(), &dir_url)
            .basic_auth(&cfg.user, Some(&cfg.pass))
            .send()
            .await;
    }

    let put = client
        .put(&file_url)
        .basic_auth(&cfg.user, Some(&cfg.pass))
        .body(bytes.to_vec())
        .send()
        .await;
    let put = put.map_err(|e| format!("cloud upload unreachable: {e}"))?;
    if !put.status().is_success() {
        return Err(format!(
            "cloud upload failed: HTTP {}",
            put.status().as_u16()
        ));
    }

    let form = [
        ("path", format!("/{rel}")),
        ("shareType", "3".to_string()),   // public link
        ("permissions", "1".to_string()), // read only
    ];
    let share = client
        .post(&cfg.share_url)
        .header("OCS-APIRequest", "true")
        .basic_auth(&cfg.user, Some(&cfg.pass))
        .form(&form)
        .send()
        .await;
    let share = share.map_err(|e| format!("share creation unreachable: {e}"))?;
    if !share.status().is_success() {
        return Err(format!(
            "share creation failed: HTTP {}",
            share.status().as_u16()
        ));
    }
    let body = share
        .bytes()
        .await
        .map_err(|e| format!("share response unreadable: {e}"))?;
    let url = parse_share_url(&body)?;
    Ok(Attachment {
        url,
        size: Some(bytes.len() as u64),
        content_type: intercom::mime_for(&name),
        name,
    })
}

fn dir_url_of(base: &str, poll_key: &str) -> String {
    format!("{}/Umfragen/{}", base.trim_end_matches('/'), poll_key)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn opts(strs: &[&str]) -> Vec<String> {
        strs.iter().map(|s| s.to_string()).collect()
    }

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("poll-{tag}-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn store_with(dir: &std::path::Path, file: &str) -> PollStore {
        PollStore::open(Some(dir.join(file)), None, None)
    }

    // ── validation ─────────────────────────────────────────────────────

    #[test]
    fn validate_trims_and_accepts() {
        let (q, o) = validate_poll("  Mittagessen? ", &opts(&[" Pizza ", "Sushi"])).unwrap();
        assert_eq!(q, "Mittagessen?");
        assert_eq!(o, vec!["Pizza".to_string(), "Sushi".to_string()]);
    }

    #[test]
    fn validate_rejects_bad_shapes() {
        assert!(validate_poll("", &opts(&["a", "b"])).is_err());
        assert!(validate_poll(&"x".repeat(MAX_QUESTION + 1), &opts(&["a", "b"])).is_err());
        assert!(validate_poll("q", &opts(&["a"])).is_err()); // too few
        assert!(validate_poll("q", &opts(&["a", "b", "", " "])).is_err()); // empty option
        let many: Vec<String> = (0..=MAX_OPTIONS).map(|i| format!("o{i}")).collect();
        assert!(validate_poll("q", &many).is_err()); // too many
        assert!(validate_poll("q", &opts(&["a", &"x".repeat(MAX_OPTION_LEN + 1)])).is_err());
    }

    // ── store ──────────────────────────────────────────────────────────

    #[test]
    fn create_vote_and_list() {
        let dir = tmpdir("basic");
        let store = PollStore::open(Some(dir.join("polls.json")), None, None);
        let p = store.create("Wo?.".into(), opts(&["a", "b"]), None);
        assert_eq!(p.votes, vec![0, 0]);
        let p = store.vote(p.id, 1).unwrap();
        assert_eq!(p.votes, vec![0, 1]);
        let listed = store.list();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].votes, vec![0, 1]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn vote_rejects_unknown_poll_option_and_closed() {
        let dir = tmpdir("vote");
        let store = PollStore::open(Some(dir.join("polls.json")), None, None);
        let p = store.create("q".into(), opts(&["a", "b"]), None);
        assert!(store.vote(999, 0).is_err());
        assert!(store.vote(p.id, 2).is_err());
        // close via direct store manipulation (no close endpoint in v1)
        store.polls.lock().unwrap()[0].closed = true;
        assert!(store.vote(p.id, 0).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn store_survives_reopen_from_disk() {
        let dir = tmpdir("persist");
        let path = dir.join("polls.json");
        {
            let store = PollStore::open(Some(path.clone()), None, None);
            store.create("frage 1".into(), opts(&["a", "b"]), None);
            let p2 = store.create("frage 2".into(), opts(&["x", "y", "z"]), None);
            store.vote(p2.id, 2).unwrap();
        }
        let store = PollStore::open(Some(path), None, None);
        let listed = store.list();
        assert_eq!(listed.len(), 2);
        assert_eq!(listed[0].question, "frage 2"); // newest first
        assert_eq!(listed[0].votes, vec![0, 0, 1]);
        // id counter continues after reload
        assert_eq!(
            store.create("frage 3".into(), opts(&["a", "b"]), None).id,
            3
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn store_fifo_cap_survives_reload() {
        let dir = tmpdir("cap");
        let path = dir.join("polls.json");
        {
            let store = PollStore::open(Some(path.clone()), None, None);
            for i in 0..(MAX_POLLS + 5) {
                store.create(format!("p{i}"), opts(&["a", "b"]), None);
            }
        }
        let store = PollStore::open(Some(path), None, None);
        let listed = store.list();
        assert_eq!(listed.len(), MAX_POLLS);
        assert!(!listed.iter().any(|p| p.question == "p0"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn store_corrupt_file_renamed_aside() {
        let dir = tmpdir("corrupt");
        let path = dir.join("polls.json");
        std::fs::write(&path, b"{not json").unwrap();
        let store = PollStore::open(Some(path.clone()), None, None);
        assert!(store.list().is_empty());
        assert!(path.with_extension("json.corrupt").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn create_carries_attachment() {
        let dir = tmpdir("att");
        let store = PollStore::open(Some(dir.join("polls.json")), None, None);
        let att = intercom::parse_attachment_metadata(
            "https://cloud.example/s/abc",
            Some("application/pdf"),
            Some(9),
            Some("attachment; filename=\"beschluss.pdf\""),
        );
        let p = store.create("Abstimmung".into(), opts(&["ja", "nein"]), Some(att));
        assert_eq!(
            store.list()[0].attachment.as_ref().unwrap().name,
            "beschluss.pdf"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── local fallback uploads ─────────────────────────────────────────

    #[test]
    fn local_upload_roundtrip_and_guards() {
        let dir = tmpdir("local");
        let store = PollStore::open(None, Some(dir.clone()), None);
        let (token, name) = store
            .save_attachment_local("Antrag 2026.pdf", b"%PDF")
            .unwrap();
        assert!(name.ends_with(".pdf"));
        let (bytes, ct) = store.read_attachment_local(&token, &name).unwrap();
        assert_eq!(bytes, b"%PDF");
        assert_eq!(ct, "application/pdf");
        assert!(store.save_attachment_local("x.txt", b"").is_err());
        let big = vec![0u8; (intercom::MAX_UPLOAD + 1) as usize];
        assert!(store.save_attachment_local("big.bin", &big).is_err());
        // traversal guards mirror the intercom reader
        assert!(store.read_attachment_local("../..", &name).is_none());
        assert!(store.read_attachment_local(&token, "../x").is_none());
        assert!(store
            .read_attachment_local("deadbeefdeadbeefdeadbeefdeadbeef", &name)
            .is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn local_upload_disabled_without_dir() {
        let store = PollStore::open(None, None, None);
        assert!(store.save_attachment_local("a.txt", b"x").is_err());
    }

    // ── cloud upload config + url building ────────────────────────────

    #[test]
    fn dav_base_host_base_strips_paths() {
        assert_eq!(
            dav_base_host_base("https://cloud.opensme.graphwiz.ai/remote.php/dav/files/uploader/"),
            "https://cloud.opensme.graphwiz.ai"
        );
        assert_eq!(
            dav_base_host_base("http://cloud.local:8099/remote.php/dav/files/u"),
            "http://cloud.local:8099"
        );
    }

    #[test]
    fn dav_file_url_builds_paths() {
        let url = dav_file_url(
            "https://cloud.opensme.graphwiz.ai/remote.php/dav/files/uploader",
            "Umfragen/abc123",
            "tok",
            "antrag.pdf",
        );
        assert_eq!(
            url,
            "https://cloud.opensme.graphwiz.ai/remote.php/dav/files/uploader/Umfragen/abc123/tok_antrag.pdf"
        );
    }

    #[test]
    fn parse_share_url_extracts_ocs_data_url() {
        let body = br#"{"ocs":{"meta":{"status":"ok"},"data":{"url":"https://cloud.opensme.graphwiz.ai/s/kJeYFw7BdAoLnHd"}}}"#;
        assert_eq!(
            parse_share_url(body).unwrap(),
            "https://cloud.opensme.graphwiz.ai/s/kJeYFw7BdAoLnHd"
        );
        assert!(parse_share_url(br#"{"ocs":{"data":{}}}"#).is_err());
        assert!(parse_share_url(b"<xml/>").is_err());
    }

    #[test]
    fn cloud_config_requires_all_three_vars() {
        // env vars leak between tests (process-global); only assert the
        // genuinely-safe direction: with vars unset-and-empty it is None.
        std::env::remove_var("CLOUD_WEBDAV_URL");
        std::env::remove_var("CLOUD_WEBDAV_USER");
        std::env::remove_var("CLOUD_WEBDAV_PASS");
        assert!(CloudUploadConfig::from_env().is_none());
    }
}
