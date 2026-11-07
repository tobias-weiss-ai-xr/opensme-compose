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
//! Vote identity: the portal has no per-user identity server-side for
//! v1 — double-vote prevention used to be client-side (localStorage
//! marker) only. Since the routes sit behind the session firewall, the
//! handlers now pass the OIDC session subject into the store: one vote
//! per subject per poll, enforced server-side (`voted_by` in the
//! persisted JSON; the public API never exposes subjects — list/vote
//! answers carry only a boolean `voted` flag for the asking user).

use crate::cloud;
use crate::intercom::{self, Attachment};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

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
    /// Login display name of the poll author (None = legacy store).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub created_by: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attachment: Option<Attachment>,
    /// Session subjects that already voted — internal + persisted only,
    /// NEVER serialized through the public API (see PollPublic).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub voted_by: Vec<String>,
}

/// Public API shape for a poll: everything a client may see — including
/// `voted: true` for the REQUESTING session (when known) so the UI can
/// disable choices server-truthfully — but never other users' subjects.
#[derive(Clone, Debug, Serialize)]
pub struct PollPublic {
    pub id: u64,
    pub question: String,
    pub options: Vec<String>,
    pub votes: Vec<u32>,
    pub closed: bool,
    pub created_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_by: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attachment: Option<Attachment>,
    /// Set when `viewer` is known: did THIS user already vote?
    #[serde(skip_serializing_if = "Option::is_none")]
    pub voted: Option<bool>,
}

impl Poll {
    /// Public view for `viewer` (session subject). `None` viewer = no
    /// `voted` flag (shape used for create responses).
    pub fn public(&self, viewer: Option<&str>) -> PollPublic {
        PollPublic {
            id: self.id,
            question: self.question.clone(),
            options: self.options.clone(),
            votes: self.votes.clone(),
            closed: self.closed,
            created_at: self.created_at.clone(),
            created_by: self.created_by.clone(),
            attachment: self.attachment.clone(),
            voted: viewer.map(|v| self.voted_by.iter().any(|s| s == v)),
        }
    }
}

/// Legacy alias: the cloud client used to live here as
/// `CloudUploadConfig` — it converged into `cloud::CloudFiles`.
pub type CloudUploadConfig = cloud::CloudFiles;

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

    /// Public API listing for `viewer` (session subject): never exposes
    /// other users' subjects, answers `voted` for the asking user only.
    pub fn list_public(&self, viewer: Option<&str>) -> Vec<PollPublic> {
        self.list().iter().map(|p| p.public(viewer)).collect()
    }

    pub fn create(
        &self,
        question: String,
        options: Vec<String>,
        attachment: Option<Attachment>,
        created_by: Option<String>,
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
            created_by,
            attachment,
            voted_by: Vec::new(),
        };
        let mut polls = self.polls.lock().unwrap_or_else(|e| e.into_inner());
        polls.push(poll.clone());
        // Rotated-out polls: their LOCAL fallback uploads die with the
        // poll (cloud share links survive — the file stays in the cloud).
        let mut dropped = Vec::new();
        while polls.len() > MAX_POLLS {
            if let Some(old) = polls.first().cloned() {
                polls.remove(0);
                dropped.push(old);
            }
        }
        drop(polls);
        self.persist(&self.snapshot());
        for old in dropped {
            if let Some(dir) = self.attachments_dir.as_ref() {
                intercom::remove_local_attachment(dir, &old.attachment, "/api/files/");
            }
        }
        poll
    }

    /// Register a vote for `voter` (session subject). One vote per
    /// subject per poll — a second attempt is rejected ("already voted");
    /// a `None` voter (internal/test) skips the double-vote guard.
    pub fn vote(&self, id: u64, option: usize, voter: Option<&str>) -> Result<Poll, String> {
        let result = {
            let mut polls = self.polls.lock().unwrap_or_else(|e| e.into_inner());
            let poll = polls
                .iter_mut()
                .find(|p| p.id == id)
                .ok_or("poll not found")?;
            if poll.closed {
                return Err("poll is closed".into());
            }
            if let Some(v) = voter {
                if poll.voted_by.iter().any(|s| s == v) {
                    return Err("already voted".into());
                }
            }
            if option >= poll.options.len() {
                return Err("invalid option".into());
            }
            poll.votes[option] += 1;
            if let Some(v) = voter {
                poll.voted_by.push(v.to_string());
            }
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
/// Umfragen/<poll_key>/, then a public read-only share via the OCS API
/// (both via the generalized `cloud::CloudFiles` client — same request
/// shape, headers and share parsing as the original inline flow).
/// Returns the attachment snapshot carrying the SHARE url.
pub async fn upload_to_cloud(
    cfg: &CloudUploadConfig,
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
    let token = intercom::random_token();
    let dir = format!("Umfragen/{poll_key}");
    let rel = format!("{dir}/{token}_{name}");

    // Ensure collections exist (RFC4918: 405 = already there).
    let _ = cfg.mkdir("Umfragen").await;
    let _ = cfg.mkdir(&dir).await;

    let content_type = intercom::mime_for(&name);
    cfg.put(&rel, bytes, &content_type)
        .await
        .map_err(|e| format!("cloud upload failed: {e}"))?;
    let url = cfg.create_share(&rel).await?;
    Ok(Attachment {
        url,
        size: Some(bytes.len() as u64),
        content_type,
        name,
    })
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
        let p = store.create("Wo?.".into(), opts(&["a", "b"]), None, None);
        assert_eq!(p.votes, vec![0, 0]);
        let p = store.vote(p.id, 1, None).unwrap();
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
        let p = store.create("q".into(), opts(&["a", "b"]), None, None);
        assert!(store.vote(999, 0, None).is_err());
        assert!(store.vote(p.id, 2, None).is_err());
        // close via direct store manipulation (no close endpoint in v1)
        store.polls.lock().unwrap()[0].closed = true;
        assert!(store.vote(p.id, 0, None).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn store_survives_reopen_from_disk() {
        let dir = tmpdir("persist");
        let path = dir.join("polls.json");
        {
            let store = PollStore::open(Some(path.clone()), None, None);
            store.create("frage 1".into(), opts(&["a", "b"]), None, None);
            let p2 = store.create("frage 2".into(), opts(&["x", "y", "z"]), None, None);
            store.vote(p2.id, 2, None).unwrap();
        }
        let store = PollStore::open(Some(path), None, None);
        let listed = store.list();
        assert_eq!(listed.len(), 2);
        assert_eq!(listed[0].question, "frage 2"); // newest first
        assert_eq!(listed[0].votes, vec![0, 0, 1]);
        // id counter continues after reload
        assert_eq!(
            store
                .create("frage 3".into(), opts(&["a", "b"]), None, None)
                .id,
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
                store.create(format!("p{i}"), opts(&["a", "b"]), None, None);
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
    fn double_vote_rejected_per_subject() {
        let dir = tmpdir("double");
        let store = PollStore::open(Some(dir.join("polls.json")), None, None);
        let p = store.create("Abend?".into(), opts(&["ja", "nein"]), None, None);
        // first vote counts
        store.vote(p.id, 0, Some("alice-sub")).unwrap();
        // SAME subject again → rejected, votes unchanged
        let err = store.vote(p.id, 1, Some("alice-sub")).unwrap_err();
        assert!(err.contains("already voted"));
        // different subject may vote (different option)
        store.vote(p.id, 1, Some("bob-sub")).unwrap();
        let listed = store.list();
        assert_eq!(listed[0].votes, vec![1, 1]);
        assert_eq!(listed[0].voted_by.len(), 2);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn voted_by_persists_and_legacy_json_loads() {
        let dir = tmpdir("persist-votes");
        let path = dir.join("polls.json");
        {
            let store = PollStore::open(Some(path.clone()), None, None);
            let p = store.create("q".into(), opts(&["a", "b"]), None, None);
            store.vote(p.id, 1, Some("carol-sub")).unwrap();
        }
        let store = PollStore::open(Some(path.clone()), None, None);
        // guard survives the reload
        assert!(store.vote(1, 0, Some("carol-sub")).is_err());
        store.vote(1, 0, Some("dave-sub")).unwrap();
        // legacy JSON (pre-author/pre-voted_by) still loads
        std::fs::write(
            &path,
            r#"[{"id":7,"question":"legacy","options":["a"],"votes":[3],"closed":false,"created_at":"1"}]"#,
        )
        .unwrap();
        let legacy = PollStore::open(Some(path), None, None);
        let polls = legacy.list();
        assert_eq!(polls[0].voted_by, Vec::<String>::new());
        assert_eq!(polls[0].created_by, None);
        // legacy voters can each vote once (migration artifact, accepted)
        assert!(legacy.vote(7, 0, Some("anyone")).is_ok());
        assert!(legacy.vote(7, 0, Some("anyone")).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn public_view_hides_subjects_and_answers_voted() {
        let dir = tmpdir("public");
        let store = PollStore::open(Some(dir.join("polls.json")), None, None);
        let p = store
            .create("Fr?".into(), opts(&["a", "b"]), None, Some("Eve".into()))
            .public(None);
        let json = serde_json::to_value(&p).unwrap();
        assert!(json.get("voted_by").is_none(), "subjects must never leak");
        assert_eq!(json["created_by"], serde_json::json!("Eve"));
        assert!(json.get("voted").is_none(), "no viewer → no voted flag");

        let store2 = PollStore::open(Some(dir.join("x.json")), None, None);
        let p2 = store2.create("Fr?".into(), opts(&["a", "b"]), None, None);
        let after_vote = store2.vote(p2.id, 0, Some("frank-sub")).unwrap();
        let for_frank = serde_json::to_value(after_vote.public(Some("frank-sub"))).unwrap();
        assert_eq!(for_frank["voted"], serde_json::json!(true));
        assert!(for_frank.get("voted_by").is_none());
        let for_greta = serde_json::to_value(p2.public(Some("greta-sub"))).unwrap();
        assert_eq!(for_greta["voted"], serde_json::json!(false));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn rotated_local_upload_files_are_gc_collected() {
        let dir = tmpdir(&format!(
            "gc-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis()
        ));
        let atts = dir.join("attachments");
        std::fs::create_dir_all(&atts).unwrap();
        let store = PollStore::open(Some(dir.join("polls.json")), Some(atts.clone()), None);
        let mut first_url = None;
        for i in 0..(MAX_POLLS + 2) {
            let bytes = format!("doc-{i}").into_bytes();
            let (token, name) = store
                .save_attachment_local(&format!("antrag-{i}.txt"), &bytes)
                .unwrap();
            let att = intercom::Attachment {
                url: format!("/api/files/{token}/{name}"),
                name: name.clone(),
                size: Some(bytes.len() as u64),
                content_type: "text/plain".into(),
            };
            if i == 0 {
                first_url = Some(att.url.clone());
            }
            store.create(format!("p{i}"), opts(&["ja", "nein"]), Some(att), None);
        }
        assert_eq!(
            std::fs::read_dir(&atts).unwrap().count(),
            MAX_POLLS,
            "rotated-out poll upload must be gc'd"
        );
        let first = first_url.unwrap();
        let rest = first.strip_prefix("/api/files/").unwrap();
        let (token, name) = rest.split_once('/').unwrap();
        assert!(!atts.join(format!("{token}_{name}")).exists());
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
        store.create("Abstimmung".into(), opts(&["ja", "nein"]), Some(att), None);
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
    // (env/parsing/URL helpers moved to cloud.rs — tested there.)

    #[tokio::test]
    async fn upload_to_cloud_guards_reject_before_any_network() {
        let cfg = cloud::CloudFiles::from_parts(
            "http://127.0.0.1:9/remote.php/dav/files/bot".into(),
            "bot".into(),
            "p".into(),
            reqwest::Client::new(),
        );
        assert_eq!(
            upload_to_cloud(&cfg, "k", "x.txt", b"").await.unwrap_err(),
            "attachment is empty"
        );
        let big = vec![0u8; (intercom::MAX_UPLOAD + 1) as usize];
        assert!(upload_to_cloud(&cfg, "k", "x.txt", &big)
            .await
            .unwrap_err()
            .contains("attachment exceeds"));
        // traversal in the poll key must fail in URL building — the
        // request is never even constructed, let alone sent
        let err = upload_to_cloud(&cfg, "../../etc", "x.txt", b"hi")
            .await
            .unwrap_err();
        assert!(err.contains("invalid cloud path"), "got: {err}");
    }
}
