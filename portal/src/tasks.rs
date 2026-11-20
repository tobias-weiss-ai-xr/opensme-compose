//! Team tasks: portal-native "Aufgaben" store (spec: capability
//! `portal-tasks`). Capped JSON-persisted store cloned from the intercom
//! pattern, minus the FIFO cap — tasks are work items, so the cap evicts
//! the oldest DONE task first and never evicts an open one.
//!
//! Persistence: TASKS_DATA_PATH holds a JSON array (atomic tmp+rename
//! write-through, reload on boot, corrupt file renamed aside). Unset =
//! in-memory only (dev default). Cap: TASKS_MAX_TASKS, clamped 10..=500,
//! default MAX_TASKS.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::RwLock;

pub const ENV_DATA_PATH: &str = "TASKS_DATA_PATH";
pub const ENV_MAX_TASKS: &str = "TASKS_MAX_TASKS";
/// Store cap — TASKS_MAX_TASKS is clamped into 10..=MAX_TASKS.
pub const MAX_TASKS: usize = 500;
pub const MAX_TITLE_LEN: usize = 200;
pub const MAX_ASSIGNEE_LEN: usize = 100;
pub const MAX_DESCRIPTION_LEN: usize = 2000;

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Priority {
    Low,
    #[default]
    Normal,
    High,
}

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum TaskStatus {
    #[default]
    Open,
    Done,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Task {
    pub id: u64,
    pub title: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    pub assignee: String,
    pub author: String,
    #[serde(default)]
    pub priority: Priority,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub due: Option<String>,
    #[serde(default)]
    pub status: TaskStatus,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_url: Option<String>,
    pub created_at: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub done_at: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct TaskCreate {
    pub title: String,
    #[serde(default)]
    pub description: Option<String>,
    pub assignee: String,
    #[serde(default)]
    pub priority: Option<String>,
    #[serde(default)]
    pub due: Option<String>,
    #[serde(default)]
    pub source_url: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct TaskPatch {
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub assignee: Option<String>,
    #[serde(default)]
    pub priority: Option<String>,
    #[serde(default)]
    pub due: Option<String>,
    #[serde(default)]
    pub status: Option<TaskStatus>,
    #[serde(default)]
    pub source_url: Option<String>,
}

#[derive(Debug, PartialEq)]
pub enum PatchError {
    NotFound,
    Invalid(String),
}

fn unix_secs() -> String {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs().to_string())
        .unwrap_or_default()
}

pub fn validate_title(raw: &str) -> Result<String, String> {
    let t = raw.trim();
    if t.is_empty() {
        return Err("Titel darf nicht leer sein".into());
    }
    if t.chars().count() > MAX_TITLE_LEN {
        return Err(format!(
            "Titel darf höchstens {} Zeichen haben",
            MAX_TITLE_LEN
        ));
    }
    Ok(t.to_string())
}

pub fn validate_assignee(raw: &str) -> Result<String, String> {
    let a = raw.trim();
    if a.is_empty() {
        return Err("Zuständiger darf nicht leer sein".into());
    }
    if a.chars().count() > MAX_ASSIGNEE_LEN {
        return Err(format!(
            "Zuständiger darf höchstens {} Zeichen haben",
            MAX_ASSIGNEE_LEN
        ));
    }
    Ok(a.to_string())
}

pub fn validate_description(raw: Option<&str>) -> Result<Option<String>, String> {
    let Some(raw) = raw else { return Ok(None) };
    let d = raw.trim();
    if d.is_empty() {
        return Ok(None);
    }
    if d.chars().count() > MAX_DESCRIPTION_LEN {
        return Err(format!(
            "Beschreibung darf höchstens {} Zeichen haben",
            MAX_DESCRIPTION_LEN
        ));
    }
    Ok(Some(d.to_string()))
}

pub fn validate_due(raw: Option<&str>) -> Result<Option<String>, String> {
    let Some(raw) = raw else { return Ok(None) };
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(None);
    }
    let bad = || "Fälligkeit muss YYYY-MM-DD sein".to_string();
    let b = raw.as_bytes();
    let shape_ok = b.len() == 10
        && b[4] == b'-'
        && b[7] == b'-'
        && b.iter()
            .enumerate()
            .all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit());
    if !shape_ok {
        return Err(bad());
    }
    let y: u32 = raw[0..4].parse().map_err(|_| bad())?;
    let m: u32 = raw[5..7].parse().map_err(|_| bad())?;
    let d: u32 = raw[8..10].parse().map_err(|_| bad())?;
    let leap = (y.is_multiple_of(4) && !y.is_multiple_of(100)) || y.is_multiple_of(400);
    let days_in_month = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    if !(1..=12).contains(&m) || d < 1 || d > days_in_month[(m - 1) as usize] {
        return Err(bad());
    }
    Ok(Some(raw.to_string()))
}

pub fn parse_priority(raw: Option<&str>) -> Result<Priority, String> {
    let Some(raw) = raw else {
        return Ok(Priority::Normal);
    };
    match raw.trim().to_ascii_lowercase().as_str() {
        "" | "normal" => Ok(Priority::Normal),
        "low" | "niedrig" => Ok(Priority::Low),
        "high" | "hoch" => Ok(Priority::High),
        other => Err(format!("Unbekannte Priorität: {other}")),
    }
}

pub fn validate_source_url(raw: Option<&str>) -> Result<Option<String>, String> {
    let Some(raw) = raw else { return Ok(None) };
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(None);
    }
    if raw.starts_with("https://") || raw.starts_with("http://") {
        Ok(Some(raw.to_string()))
    } else {
        Err("Link muss mit http(s):// beginnen".into())
    }
}

/// Only the assignee or the author may mark a task done or delete it.
/// Matching is case-insensitive against the session email OR name.
pub fn may_complete(session_email: &str, session_name: &str, task: &Task) -> bool {
    let matches = |v: &str| {
        let v = v.trim();
        v.eq_ignore_ascii_case(session_email.trim()) || v.eq_ignore_ascii_case(session_name.trim())
    };
    matches(&task.assignee) || matches(&task.author)
}

/// Filter tokens are AND-ed: "all" (default), "mine", "open", "done",
/// comma-combinable ("mine,open"). Unknown tokens are ignored.
pub fn apply_filter(tasks: &[Task], filter: &str, me: &[String]) -> Vec<Task> {
    let mut tokens: Vec<&str> = filter
        .split(',')
        .map(|t| t.trim())
        .filter(|t| !t.is_empty())
        .collect();
    if tokens.is_empty() {
        tokens.push("all");
    }
    tasks
        .iter()
        .filter(|t| {
            tokens.iter().all(|tok| match *tok {
                "all" => true,
                "open" => t.status == TaskStatus::Open,
                "done" => t.status == TaskStatus::Done,
                "mine" => me
                    .iter()
                    .any(|m| t.assignee.trim().eq_ignore_ascii_case(m.trim())),
                _ => true,
            })
        })
        .cloned()
        .collect()
}

/// Distinct, sorted known members: task authors/assignees plus extra
/// identities (e.g. note authors). Case-insensitive dedupe, first-seen
/// spelling kept.
pub fn users_from(tasks: &[Task], extra: &[String]) -> Vec<String> {
    let mut seen: Vec<String> = Vec::new();
    let mut push = |v: &str| {
        let v = v.trim();
        if v.is_empty() {
            return;
        }
        if !seen.iter().any(|s| s.eq_ignore_ascii_case(v)) {
            seen.push(v.to_string());
        }
    };
    for t in tasks {
        push(&t.author);
        push(&t.assignee);
    }
    for e in extra {
        push(e);
    }
    seen.sort();
    seen
}

pub struct TaskStore {
    /// oldest-first
    tasks: RwLock<Vec<Task>>,
    next_id: AtomicU64,
    max_tasks: usize,
    /// JSON persistence file; None = in-memory only.
    data_path: Option<PathBuf>,
}

impl Default for TaskStore {
    fn default() -> Self {
        Self::open(None, MAX_TASKS)
    }
}

impl TaskStore {
    /// Build a store persisting to `data_path` with the given cap. Loads
    /// existing tasks (missing file = fresh store; corrupt file = rename
    /// aside, start fresh).
    pub fn open(data_path: Option<PathBuf>, max_tasks: usize) -> Self {
        let store = Self {
            tasks: RwLock::new(Vec::new()),
            next_id: AtomicU64::new(1),
            max_tasks,
            data_path,
        };
        store.load();
        store
    }

    fn load(&self) {
        let Some(path) = &self.data_path else { return };
        let Ok(raw) = std::fs::read(path) else { return };
        match serde_json::from_slice::<Vec<Task>>(&raw) {
            Ok(tasks) => {
                let next = tasks.iter().map(|t| t.id).max().unwrap_or(0) + 1;
                self.next_id.store(next, Ordering::Relaxed);
                *self.tasks.write().unwrap_or_else(|e| e.into_inner()) = tasks;
            }
            Err(_) => {
                // Corrupt store: keep it for forensics, start fresh.
                let _ = std::fs::rename(path, path.with_extension("json.corrupt"));
            }
        }
    }

    /// Atomic write-through: serialize then rename over the target.
    fn persist(&self, tasks: &[Task]) {
        let Some(path) = &self.data_path else { return };
        let Ok(json) = serde_json::to_vec(tasks) else {
            return;
        };
        let tmp = path.with_extension("json.tmp");
        if std::fs::write(&tmp, json).is_ok() {
            let _ = std::fs::rename(&tmp, path);
        }
    }

    pub fn create(&self, input: TaskCreate, author: String) -> Result<Task, String> {
        let title = validate_title(&input.title)?;
        let assignee = validate_assignee(&input.assignee)?;
        let description = validate_description(input.description.as_deref())?;
        let priority = parse_priority(input.priority.as_deref())?;
        let due = validate_due(input.due.as_deref())?;
        let source_url = validate_source_url(input.source_url.as_deref())?;
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let task = Task {
            id,
            title,
            description,
            assignee,
            author,
            priority,
            due,
            status: TaskStatus::Open,
            source_url,
            created_at: unix_secs(),
            done_at: None,
        };
        let mut tasks = self.tasks.write().unwrap_or_else(|e| e.into_inner());
        tasks.push(task.clone());
        // Cap: evict the oldest DONE task while over cap. Open tasks are
        // never evicted — if everything is open, the store simply grows
        // past the cap until something is completed.
        while tasks.len() > self.max_tasks {
            match tasks.iter().position(|t| t.status == TaskStatus::Done) {
                Some(pos) => {
                    tasks.remove(pos);
                }
                None => break,
            }
        }
        let snapshot = tasks.clone();
        drop(tasks);
        self.persist(&snapshot);
        Ok(task)
    }

    pub fn patch(&self, id: u64, patch: TaskPatch) -> Result<Task, PatchError> {
        // Validate everything before mutating anything.
        let title = match &patch.title {
            Some(t) => Some(validate_title(t).map_err(PatchError::Invalid)?),
            None => None,
        };
        let description =
            validate_description(patch.description.as_deref()).map_err(PatchError::Invalid)?;
        let assignee = match &patch.assignee {
            Some(a) => Some(validate_assignee(a).map_err(PatchError::Invalid)?),
            None => None,
        };
        let priority = parse_priority(patch.priority.as_deref()).map_err(PatchError::Invalid)?;
        let due = validate_due(patch.due.as_deref()).map_err(PatchError::Invalid)?;
        let source_url =
            validate_source_url(patch.source_url.as_deref()).map_err(PatchError::Invalid)?;
        let status = patch.status;

        let mut tasks = self.tasks.write().unwrap_or_else(|e| e.into_inner());
        let Some(idx) = tasks.iter().position(|t| t.id == id) else {
            return Err(PatchError::NotFound);
        };
        let t = &mut tasks[idx];
        if let Some(v) = title {
            t.title = v;
        }
        if patch.description.is_some() {
            t.description = description;
        }
        if let Some(v) = assignee {
            t.assignee = v;
        }
        if patch.priority.is_some() {
            t.priority = priority;
        }
        if patch.due.is_some() {
            t.due = due;
        }
        if patch.source_url.is_some() {
            t.source_url = source_url;
        }
        if let Some(s) = status {
            if s == TaskStatus::Done && t.status != TaskStatus::Done {
                t.done_at = Some(unix_secs());
            }
            if s == TaskStatus::Open {
                t.done_at = None;
            }
            t.status = s;
        }
        let out = t.clone();
        let snapshot = tasks.clone();
        drop(tasks);
        self.persist(&snapshot);
        Ok(out)
    }

    pub fn delete(&self, id: u64) -> bool {
        let mut tasks = self.tasks.write().unwrap_or_else(|e| e.into_inner());
        let Some(pos) = tasks.iter().position(|t| t.id == id) else {
            return false;
        };
        tasks.remove(pos);
        let snapshot = tasks.clone();
        drop(tasks);
        self.persist(&snapshot);
        true
    }

    pub fn get(&self, id: u64) -> Option<Task> {
        self.tasks
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .find(|t| t.id == id)
            .cloned()
    }

    /// Newest-first full list.
    pub fn list(&self) -> Vec<Task> {
        self.tasks
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .rev()
            .cloned()
            .collect()
    }

    /// Delta listing: newest-first tasks with id > `since`.
    pub fn list_since(&self, since: u64) -> Vec<Task> {
        self.tasks
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .rev()
            .filter(|t| t.id > since)
            .cloned()
            .collect()
    }

    /// Highest id currently held (0 on an empty store).
    pub fn latest_id(&self) -> u64 {
        self.tasks
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .last()
            .map(|t| t.id)
            .unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_SEQ: AtomicU64 = AtomicU64::new(1);

    fn test_path() -> std::path::PathBuf {
        let n = TEST_SEQ.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "opensme-tasks-test-{}-{}.json",
            std::process::id(),
            n
        ))
    }

    fn cleanup(path: &std::path::Path) {
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_file(path.with_extension("json.tmp"));
        let _ = std::fs::remove_file(path.with_extension("json.corrupt"));
    }

    fn create_input(title: &str, assignee: &str) -> TaskCreate {
        TaskCreate {
            title: title.to_string(),
            description: None,
            assignee: assignee.to_string(),
            priority: None,
            due: None,
            source_url: None,
        }
    }

    // ── validation ────────────────────────────────────────────────

    #[test]
    fn validate_title_trims_and_bounds() {
        assert!(validate_title("   ").is_err());
        assert!(validate_title("").is_err());
        assert_eq!(validate_title("  Einkaufen  ").unwrap(), "Einkaufen");
        let long = "x".repeat(MAX_TITLE_LEN + 1);
        assert!(validate_title(&long).is_err());
        let max = "x".repeat(MAX_TITLE_LEN);
        assert!(validate_title(&max).is_ok());
    }

    #[test]
    fn validate_assignee_bounds() {
        assert!(validate_assignee(" ").is_err());
        assert_eq!(
            validate_assignee(" anna@firma.de ").unwrap(),
            "anna@firma.de"
        );
        let long = "a".repeat(MAX_ASSIGNEE_LEN + 1);
        assert!(validate_assignee(&long).is_err());
    }

    #[test]
    fn validate_due_accepts_only_iso_dates() {
        assert_eq!(validate_due(None).unwrap(), None);
        assert_eq!(
            validate_due(Some("2026-12-24")).unwrap(),
            Some("2026-12-24".to_string())
        );
        assert_eq!(validate_due(Some("")).unwrap(), None, "empty due = no due");
        for bad in [
            "tomorrow",
            "2026/12/24",
            "2026-13-01",
            "2026-02-30",
            "2026-00-10",
            "2026-1-1",
        ] {
            assert!(
                validate_due(Some(bad)).is_err(),
                "due {bad:?} must be rejected"
            );
        }
        // leap day valid in 2028, invalid in 2026
        assert!(validate_due(Some("2028-02-29")).is_ok());
        assert!(validate_due(Some("2026-02-29")).is_err());
    }

    #[test]
    fn parse_priority_enum_or_error() {
        assert_eq!(parse_priority(None).unwrap(), Priority::Normal);
        assert_eq!(parse_priority(Some("low")).unwrap(), Priority::Low);
        assert_eq!(parse_priority(Some("HIGH")).unwrap(), Priority::High);
        assert!(parse_priority(Some("urgent")).is_err());
    }

    #[test]
    fn validate_source_url_http_only() {
        assert_eq!(validate_source_url(None).unwrap(), None);
        assert!(validate_source_url(Some("https://cloud.example/s/abc")).is_ok());
        assert!(validate_source_url(Some("http://news.internal/a")).is_ok());
        assert_eq!(
            validate_source_url(Some("")).unwrap(),
            None,
            "empty url = no url"
        );
        for bad in ["javascript:alert(1)", "ftp://x", "cloud.example/s/abc"] {
            assert!(
                validate_source_url(Some(bad)).is_err(),
                "url {bad:?} must be rejected"
            );
        }
    }

    #[test]
    fn validate_description_bounds() {
        assert_eq!(validate_description(None).unwrap(), None);
        assert_eq!(
            validate_description(Some("  hi ")).unwrap(),
            Some("hi".to_string())
        );
        let long = "x".repeat(MAX_DESCRIPTION_LEN + 1);
        assert!(validate_description(Some(&long)).is_err());
    }

    // ── permissions ───────────────────────────────────────────────

    fn task_with(author: &str, assignee: &str) -> Task {
        Task {
            id: 1,
            title: "t".into(),
            description: None,
            assignee: assignee.into(),
            author: author.into(),
            priority: Priority::Normal,
            due: None,
            status: TaskStatus::Open,
            source_url: None,
            created_at: "0".into(),
            done_at: None,
        }
    }

    #[test]
    fn may_complete_matches_assignee_or_author_by_email_or_name() {
        let t = task_with("Anna Beispiel", "bob@firma.de");
        // assignee matches by email
        assert!(may_complete("bob@firma.de", "Bob", &t));
        // author matches by name, case-insensitive
        assert!(may_complete("x@y.de", "anna beispiel", &t));
        // unrelated member: no
        assert!(!may_complete("caro@firma.de", "Caro", &t));
    }

    // ── store: create / list / since / latest ─────────────────────

    #[test]
    fn create_stamps_author_validates_and_lists_newest_first() {
        let store = TaskStore::open(None, MAX_TASKS);
        let t = store
            .create(
                create_input("  Rechnung prüfen ", "bob@firma.de"),
                "anna@firma.de".into(),
            )
            .unwrap();
        assert_eq!(t.title, "Rechnung prüfen");
        assert_eq!(t.author, "anna@firma.de");
        assert_eq!(t.status, TaskStatus::Open);
        assert_eq!(t.priority, Priority::Normal);
        assert!(t.done_at.is_none());
        let bad = store.create(create_input("", "bob"), "anna@firma.de".into());
        assert!(bad.is_err());
        let t2 = store
            .create(
                create_input("Zweites", "bob@firma.de"),
                "anna@firma.de".into(),
            )
            .unwrap();
        let all = store.list();
        assert_eq!(all.len(), 2);
        assert_eq!(all[0].id, t2.id, "newest first");
        assert_eq!(store.list_since(t.id).len(), 1);
        assert_eq!(store.latest_id(), t2.id);
    }

    #[test]
    fn patch_applies_partial_fields_and_sets_done_at() {
        let store = TaskStore::open(None, MAX_TASKS);
        let t = store
            .create(create_input("A", "bob@firma.de"), "anna@firma.de".into())
            .unwrap();
        let done = store
            .patch(
                t.id,
                TaskPatch {
                    title: None,
                    description: Some("erledigt weil".into()),
                    assignee: None,
                    due: Some("2026-12-01".into()),
                    priority: Some("high".into()),
                    status: Some(TaskStatus::Done),
                    source_url: None,
                },
            )
            .unwrap();
        assert_eq!(done.status, TaskStatus::Done);
        assert!(done.done_at.is_some());
        assert_eq!(done.priority, Priority::High);
        assert_eq!(done.due.as_deref(), Some("2026-12-01"));
        assert_eq!(done.description.as_deref(), Some("erledigt weil"));
        // reopen clears done_at
        let reopened = store
            .patch(
                t.id,
                TaskPatch {
                    status: Some(TaskStatus::Open),
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(reopened.status, TaskStatus::Open);
        assert!(reopened.done_at.is_none());
        // invalid patch is an error and changes nothing
        let err = store.patch(
            t.id,
            TaskPatch {
                title: Some("".into()),
                ..Default::default()
            },
        );
        assert!(matches!(err, Err(PatchError::Invalid(_))));
        assert_eq!(store.get(t.id).unwrap().title, "A");
        // unknown id
        assert!(matches!(
            store.patch(999, TaskPatch::default()),
            Err(PatchError::NotFound)
        ));
    }

    #[test]
    fn delete_removes_and_reports() {
        let store = TaskStore::open(None, MAX_TASKS);
        let t = store
            .create(create_input("A", "bob"), "anna".into())
            .unwrap();
        assert!(store.delete(t.id));
        assert!(!store.delete(t.id));
        assert!(store.get(t.id).is_none());
    }

    // ── cap: evict oldest DONE first, never open ──────────────────

    #[test]
    fn cap_evicts_oldest_done_first_and_never_open() {
        let store = TaskStore::open(None, 3);
        let t1 = store
            .create(create_input("open1", "a"), "x".into())
            .unwrap();
        let t2 = store
            .create(create_input("open2", "a"), "x".into())
            .unwrap();
        let t3 = store
            .create(create_input("done1", "a"), "x".into())
            .unwrap();
        store
            .patch(
                t3.id,
                TaskPatch {
                    status: Some(TaskStatus::Done),
                    ..Default::default()
                },
            )
            .unwrap();
        // at cap (3); adding a 4th evicts t3 (the only done), keeps open t1/t2
        let t4 = store
            .create(create_input("open3", "a"), "x".into())
            .unwrap();
        let ids: Vec<u64> = store.list().iter().map(|t| t.id).collect();
        assert!(!ids.contains(&t3.id), "done task must be evicted at cap");
        assert!(ids.contains(&t1.id) && ids.contains(&t2.id) && ids.contains(&t4.id));
        // mark t1 done, add another → oldest done (t1) evicted
        store
            .patch(
                t1.id,
                TaskPatch {
                    status: Some(TaskStatus::Done),
                    ..Default::default()
                },
            )
            .unwrap();
        let t5 = store
            .create(create_input("open4", "a"), "x".into())
            .unwrap();
        let ids: Vec<u64> = store.list().iter().map(|t| t.id).collect();
        assert!(!ids.contains(&t1.id));
        assert!(ids.contains(&t2.id) && ids.contains(&t4.id) && ids.contains(&t5.id));
    }

    #[test]
    fn cap_never_evicts_open_even_when_all_open() {
        let store = TaskStore::open(None, 2);
        for i in 0..4 {
            store
                .create(create_input(&format!("open{i}"), "a"), "x".into())
                .unwrap();
        }
        assert_eq!(
            store.list().len(),
            4,
            "open tasks must survive beyond the cap"
        );
    }

    // ── persistence ───────────────────────────────────────────────

    #[test]
    fn persistence_roundtrip_and_id_continuity() {
        let path = test_path();
        {
            let store = TaskStore::open(Some(path.clone()), MAX_TASKS);
            let t = store
                .create(
                    create_input("Persistiert", "bob@firma.de"),
                    "anna@firma.de".into(),
                )
                .unwrap();
            store
                .patch(
                    t.id,
                    TaskPatch {
                        status: Some(TaskStatus::Done),
                        ..Default::default()
                    },
                )
                .unwrap();
            assert!(path.exists());
        }
        {
            let store = TaskStore::open(Some(path.clone()), MAX_TASKS);
            let all = store.list();
            assert_eq!(all.len(), 1);
            assert_eq!(all[0].title, "Persistiert");
            assert_eq!(all[0].status, TaskStatus::Done);
            assert!(all[0].done_at.is_some());
            // id sequence continues past restored max
            let t2 = store
                .create(create_input("Neu", "bob"), "anna".into())
                .unwrap();
            assert!(t2.id > all[0].id);
        }
        cleanup(&path);
    }

    #[test]
    fn corrupt_file_is_renamed_aside_and_store_starts_fresh() {
        let path = test_path();
        std::fs::write(&path, b"{not json").unwrap();
        let store = TaskStore::open(Some(path.clone()), MAX_TASKS);
        assert!(store.list().is_empty());
        assert!(path.with_extension("json.corrupt").exists());
        assert!(!path.exists());
        cleanup(&path);
    }

    // ── serde: legacy/partial JSON loads with defaults ────────────

    #[test]
    fn task_deserializes_with_defaults_for_missing_fields() {
        let raw = r#"{"id":7,"title":"Alt","assignee":"bob","author":"anna","created_at":"1"}"#;
        let t: Task = serde_json::from_str(raw).unwrap();
        assert_eq!(t.status, TaskStatus::Open);
        assert_eq!(t.priority, Priority::Normal);
        assert!(t.due.is_none() && t.done_at.is_none() && t.source_url.is_none());
        // roundtrip keeps stable JSON shape
        let again: Task = serde_json::from_str(&serde_json::to_string(&t).unwrap()).unwrap();
        assert_eq!(t, again);
    }

    // ── filter + known users ──────────────────────────────────────

    #[test]
    fn apply_filter_supports_all_mine_open_done_and_combinations() {
        let a = task_with("anna@firma.de", "anna@firma.de");
        let mut b = task_with("bob@firma.de", "bob@firma.de");
        b.id = 2;
        b.status = TaskStatus::Done;
        let tasks = vec![a.clone(), b];
        let me = vec!["anna@firma.de".to_string(), "Anna".to_string()];
        assert_eq!(apply_filter(&tasks, "all", &me).len(), 2);
        assert_eq!(apply_filter(&tasks, "", &me).len(), 2, "empty filter = all");
        let mine = apply_filter(&tasks, "mine", &me);
        assert_eq!(mine.len(), 1);
        assert_eq!(mine[0].id, 1);
        assert_eq!(apply_filter(&tasks, "open", &me).len(), 1);
        assert_eq!(apply_filter(&tasks, "done", &me).len(), 1);
        let mine_open = apply_filter(&tasks, "mine,open", &me);
        assert_eq!(mine_open.len(), 1);
        let mine_done = apply_filter(&tasks, "mine,done", &me);
        assert!(mine_done.is_empty(), "filter tokens are AND-ed");
    }

    #[test]
    fn users_from_dedupes_sorts_and_skips_empty() {
        let mut a = task_with("anna@firma.de", "Bob");
        a.id = 1;
        let mut b = task_with("bob@firma.de", "anna@firma.de");
        b.id = 2;
        let users = users_from(&[a, b], &["".to_string(), "caro@firma.de".to_string()]);
        assert_eq!(
            users,
            vec!["Bob", "anna@firma.de", "bob@firma.de", "caro@firma.de"]
        );
        // case-insensitive dedupe keeps first-seen spelling
        let dup = users_from(&[task_with("Anna@Firma.de", "anna@firma.de")], &[]);
        assert_eq!(dup, vec!["Anna@Firma.de"]);
    }
}
