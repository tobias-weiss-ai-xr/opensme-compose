//! Real-time event backbone for the portal (slice 1 of suite-wide intercom).
//! Events are appended to one Redis Stream; the browser reads them through an
//! SSE endpoint that filters by visibility AFTER authenticating the session.

use axum::body::Body;
use axum::http::{header, StatusCode};
use axum::response::IntoResponse;
use serde::Serialize;
use std::time::{SystemTime, UNIX_EPOCH};

pub const DEFAULT_STREAM: &str = "opensme:events";
/// Retained for config compatibility only; SSE reads use per-connection XREAD.
pub const DEFAULT_GROUP: &str = "portal-sse";
pub const DEFAULT_MAXLEN: usize = 10_000;

#[derive(Clone, Debug, PartialEq)]
pub enum Visibility {
    /// Every authenticated member may see the event (team notes).
    Team,
    /// Only these OIDC `sub` values may see it (task author + assignee).
    Recipients(Vec<String>),
}

/// The signed-in viewer, as the task/intercom stores identify them.
///
/// The stores stamp authors/assignees with email (or the display name as a
/// fallback), while the OIDC `sub` is an opaque token. Matching against all
/// three identifiers keeps recipient-scoped events deliverable without a
/// separate email→sub lookup service.
#[derive(Clone, Debug)]
pub struct Viewer {
    pub sub: String,
    pub email: String,
    pub name: String,
    pub tenant: String,
}

impl Viewer {
    fn matches(&self, id: &str) -> bool {
        self.sub == id || self.email.eq_ignore_ascii_case(id) || self.name.eq_ignore_ascii_case(id)
    }
}

impl Visibility {
    pub fn visible_to(&self, viewer: &Viewer) -> bool {
        match self {
            Visibility::Team => true,
            Visibility::Recipients(ids) => ids.iter().any(|id| viewer.matches(id)),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Event {
    /// Redis stream ID (`<ms>-<seq>`) — becomes the SSE `id:`.
    pub id: String,
    pub kind: String,
    pub actor_sub: String,
    pub tenant: String,
    #[serde(skip)]
    pub visibility: Visibility,
    pub payload: serde_json::Value,
    pub created_at: i64,
}

impl Event {
    pub fn new(
        kind: &str,
        actor_sub: &str,
        tenant: &str,
        visibility: Visibility,
        payload: serde_json::Value,
    ) -> Self {
        Self {
            id: String::new(),
            kind: kind.into(),
            actor_sub: actor_sub.into(),
            tenant: tenant.into(),
            visibility,
            payload,
            created_at: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0),
        }
    }
}

/// Serialize an event into an SSE frame. `visibility` is `#[serde(skip)]`, so
/// no recipient metadata reaches the browser.
pub fn sse_frame(event: &Event) -> String {
    format!(
        "event: {}\nid: {}\ndata: {}\n\n",
        event.kind,
        event.id,
        serde_json::to_string(event).unwrap_or_else(|_| "{}".into())
    )
}

/// Server-side authorization filter. Must run BEFORE `sse_frame` for every event.
/// Tenant is checked too: the stream is shared with future producers, and a
/// Team event from another tenant must never reach this deployment's viewers.
pub fn filter_visible(events: Vec<Event>, viewer: &Viewer) -> Vec<Event> {
    events
        .into_iter()
        .filter(|e| e.tenant == viewer.tenant && e.visibility.visible_to(viewer))
        .collect()
}

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

#[derive(Clone, Debug, PartialEq)]
pub struct StreamConfig {
    pub enabled: bool,
    pub url: String,
    pub stream: String,
    /// Kept for config compatibility; the XREAD read path does not use it.
    pub group: String,
    pub maxlen: usize,
}

impl StreamConfig {
    /// `get` is injected so unit tests need no process env.
    pub fn from_env<F: Fn(&str) -> Option<String>>(get: F) -> Self {
        Self {
            enabled: matches!(get("EVENTS_ENABLED").as_deref(),
                Some(v) if v == "1" || v.eq_ignore_ascii_case("true")),
            url: get("REDIS_URL").unwrap_or_else(|| "redis://:password@redis:6379".into()),
            stream: get("EVENTS_STREAM").unwrap_or_else(|| DEFAULT_STREAM.into()),
            group: DEFAULT_GROUP.into(),
            maxlen: get("EVENTS_MAXLEN")
                .and_then(|v| v.parse().ok())
                .unwrap_or(DEFAULT_MAXLEN),
        }
    }
}

#[derive(Clone)]
enum BusInner {
    Disabled,
    Memory {
        counter: Arc<AtomicU64>,
        events: Arc<tokio::sync::Mutex<Vec<Event>>>,
    },
    Redis {
        client: redis::Client,
        config: StreamConfig,
    },
}

#[derive(Clone)]
pub struct EventBus(BusInner);

impl EventBus {
    pub fn disabled() -> Self {
        Self(BusInner::Disabled)
    }

    pub fn memory() -> Self {
        Self(BusInner::Memory {
            counter: Arc::new(AtomicU64::new(0)),
            events: Arc::new(tokio::sync::Mutex::new(Vec::new())),
        })
    }

    /// Build the bus from config. A malformed Redis URL degrades to disabled.
    pub fn from_config(cfg: StreamConfig) -> Self {
        if !cfg.enabled {
            return Self::disabled();
        }
        match redis::Client::open(cfg.url.clone()) {
            Ok(client) => Self(BusInner::Redis {
                client,
                config: cfg,
            }),
            Err(_) => Self::disabled(),
        }
    }

    /// Append an event; returns the stream ID (or a memory placeholder).
    /// Fail-soft: a Redis error is logged and swallowed — writes never break.
    pub async fn publish(&self, event: &Event) -> Option<String> {
        match &self.0 {
            BusInner::Disabled => None,
            BusInner::Memory { counter, events } => {
                let n = counter.fetch_add(1, Ordering::SeqCst) + 1;
                let mut e = event.clone();
                e.id = format!("m-{n}");
                events.lock().await.push(e);
                Some(format!("m-{n}"))
            }
            BusInner::Redis { client, config } => {
                let mut conn = match client.get_multiplexed_async_connection().await {
                    Ok(c) => c,
                    Err(e) => {
                        tracing::warn!(error = %e, "event bus: Redis connect failed");
                        return None;
                    }
                };
                let fields = [
                    ("kind", event.kind.as_str()),
                    ("actor_sub", event.actor_sub.as_str()),
                    ("tenant", event.tenant.as_str()),
                    (
                        "payload",
                        &serde_json::to_string(&event.payload).unwrap_or_else(|_| "{}".into()),
                    ),
                    ("created_at", &event.created_at.to_string()),
                    ("visibility", &visibility_to_json(&event.visibility)),
                ];
                let id: redis::RedisResult<String> = redis::cmd("XADD")
                    .arg(&config.stream)
                    .arg("MAXLEN")
                    .arg("~")
                    .arg(config.maxlen)
                    .arg("*")
                    .arg(&fields)
                    .query_async(&mut conn)
                    .await;
                match id {
                    Ok(id) => Some(id),
                    Err(e) => {
                        tracing::warn!(error = %e, "event bus: XADD failed");
                        None
                    }
                }
            }
        }
    }

    pub fn is_disabled(&self) -> bool {
        matches!(self.0, BusInner::Disabled)
    }

    /// Used by tests and by the SSE reader for backfill on reconnect.
    pub async fn replay(&self, _since: Option<&str>) -> Result<Vec<Event>, String> {
        match &self.0 {
            BusInner::Disabled => Ok(Vec::new()),
            BusInner::Memory { events, .. } => Ok(events.lock().await.clone()),
            BusInner::Redis { .. } => Ok(Vec::new()), // live reads use EventReader
        }
    }

    /// Browser entry point: memory bus replays its recorded events, Redis bus
    /// opens the live XREAD stream. Never panics; disabled returns 204.
    pub async fn stream(
        &self,
        viewer: Viewer,
        last_id: Option<String>,
    ) -> axum::response::Response {
        match &self.0 {
            BusInner::Disabled => StatusCode::NO_CONTENT.into_response(),
            BusInner::Memory { .. } => {
                // In-process contract tests can exercise replay; production uses Redis.
                let events = self.replay(None).await.unwrap_or_default();
                let body = filter_visible(events, &viewer)
                    .iter()
                    .map(sse_frame)
                    .collect::<Vec<_>>()
                    .join("");
                ([(header::CONTENT_TYPE, "text/event-stream")], body).into_response()
            }
            BusInner::Redis { client, config } => {
                stream_events(client.clone(), config.clone(), viewer, last_id).await
            }
        }
    }
}

fn visibility_to_json(v: &Visibility) -> String {
    match v {
        Visibility::Team => "team".into(),
        Visibility::Recipients(ids) => serde_json::to_string(ids).unwrap_or_else(|_| "[]".into()),
    }
}

fn visibility_from_str(raw: &str) -> Visibility {
    if raw == "team" {
        Visibility::Team
    } else {
        let ids: Vec<String> = serde_json::from_str(raw).unwrap_or_default();
        Visibility::Recipients(ids)
    }
}

/// Bound the SSE tasks one authenticated viewer can pin. Exceeding the cap
/// returns 429; EventSource retries with backoff and polling stays active
/// until a stream is established.
const MAX_STREAMS_PER_VIEWER: usize = 3;

static STREAM_SLOTS: std::sync::OnceLock<
    std::sync::Mutex<std::collections::HashMap<String, usize>>,
> = std::sync::OnceLock::new();

fn stream_slots() -> &'static std::sync::Mutex<std::collections::HashMap<String, usize>> {
    STREAM_SLOTS.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()))
}

struct StreamSlot(String);

impl StreamSlot {
    fn acquire(viewer: &str) -> Option<Self> {
        let mut slots = stream_slots().lock().ok()?;
        let count = slots.entry(viewer.to_string()).or_insert(0);
        if *count >= MAX_STREAMS_PER_VIEWER {
            return None;
        }
        *count += 1;
        Some(Self(viewer.to_string()))
    }
}

impl Drop for StreamSlot {
    fn drop(&mut self) {
        if let Ok(mut slots) = stream_slots().lock() {
            if let std::collections::hash_map::Entry::Occupied(mut entry) =
                slots.entry(self.0.clone())
            {
                *entry.get_mut() -= 1;
                if *entry.get() == 0 {
                    entry.remove();
                }
            }
        }
    }
}

/// A browser-supplied cursor is only a read offset. Accept Redis stream IDs
/// (`<ms>-<seq>`) and reset anything else so a malformed/crafted value cannot
/// wedge the reader in an endless error loop.
fn sanitize_cursor(last_id: Option<String>) -> String {
    match last_id {
        Some(id) if valid_stream_id(&id) => id,
        _ => "$".into(),
    }
}

fn valid_stream_id(id: &str) -> bool {
    let mut parts = id.split('-');
    match (parts.next(), parts.next(), parts.next()) {
        (Some(ms), Some(seq), None) => {
            !ms.is_empty()
                && !seq.is_empty()
                && ms.bytes().all(|b| b.is_ascii_digit())
                && seq.bytes().all(|b| b.is_ascii_digit())
        }
        _ => false,
    }
}

/// Numeric ordering key for stream IDs, used to detect a cursor that is ahead
/// of the current stream (recreated stream or changed EVENTS_STREAM).
fn stream_id_key(id: &str) -> Option<(u128, u128)> {
    let (ms, seq) = id.split_once('-')?;
    Some((ms.parse().ok()?, seq.parse().ok()?))
}

/// Minimal SSE reader over the Redis Stream for one browser connection.
/// Each connection owns its XREAD cursor (Last-Event-ID on reconnect, `$` on
/// first connect). No consumer group: a group would load-balance entries
/// across viewers, dropping events for all but one connection.
pub async fn stream_events(
    client: redis::Client,
    config: StreamConfig,
    viewer: Viewer,
    last_id: Option<String>,
) -> axum::response::Response {
    let Some(slot) = StreamSlot::acquire(&viewer.sub) else {
        tracing::warn!(sub = %viewer.sub, "events: per-viewer stream cap reached");
        return StatusCode::TOO_MANY_REQUESTS.into_response();
    };
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<String, std::io::Error>>(16);
    let mut reader = EventReader::new(client, config, sanitize_cursor(last_id));
    tokio::spawn(async move {
        // Keep the slot alive for the whole stream (dropped on client abort).
        let _slot = slot;
        let mut errors = 0u32;
        let mut ready = false;
        let mut cursor_checked = false;
        loop {
            if !cursor_checked {
                reader.ensure_cursor_live().await;
                cursor_checked = true;
            }
            match reader.next_batch().await {
                Ok(events) => {
                    errors = 0;
                    for event in filter_visible(events, &viewer) {
                        if tx.send(Ok(sse_frame(&event))).await.is_err() {
                            return;
                        }
                    }
                    // The first successful XREAD means the cursor is live. Only
                    // now may the client pause polling; `onopen` alone fires
                    // before the reader task has issued XREAD.
                    if !ready {
                        if tx
                            .send(Ok("event: ready\ndata: {}\n\n".into()))
                            .await
                            .is_err()
                        {
                            return;
                        }
                        ready = true;
                    }
                }
                Err(e) => {
                    errors += 1;
                    tracing::warn!(error = %e, "events: stream read failed");
                    if errors >= 3 {
                        // Terminating the body fires EventSource.onerror, which
                        // resumes the poller and lets the browser reconnect
                        // from Last-Event-ID once Redis is back.
                        let _ = tx
                            .send(Err(std::io::Error::other(format!(
                                "event stream unavailable: {e}"
                            ))))
                            .await;
                        return;
                    }
                }
            }
            if tx.send(Ok(": keep-alive\n\n".into())).await.is_err() {
                return;
            }
        }
    });
    let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
    (
        [
            (header::CONTENT_TYPE, "text/event-stream"),
            (header::CACHE_CONTROL, "no-cache, no-transform"),
            (header::CONNECTION, "keep-alive"),
            (header::HeaderName::from_static("x-accel-buffering"), "no"),
        ],
        Body::from_stream(stream),
    )
        .into_response()
}

pub struct EventReader {
    client: redis::Client,
    config: StreamConfig,
    cursor: String, // Redis stream ID (or "$" for new entries only)
    conn: Option<redis::aio::MultiplexedConnection>,
}

impl EventReader {
    pub fn new(client: redis::Client, config: StreamConfig, cursor: String) -> Self {
        Self {
            client,
            config,
            cursor,
            conn: None,
        }
    }

    /// A format-valid `Last-Event-ID` can still be ahead of the current stream
    /// if Redis was recreated or EVENTS_STREAM changed. XREAD from a future ID
    /// returns Nil forever while still emitting `ready`, which would silently
    /// keep the browser's poller paused. Reset to `$` in that case.
    async fn ensure_cursor_live(&mut self) {
        if self.cursor == "$" {
            return;
        }
        if self.conn.is_none() {
            self.conn = match self.client.get_multiplexed_async_connection().await {
                Ok(conn) => Some(conn),
                Err(_) => return,
            };
        }
        let Some(conn) = self.conn.as_mut() else {
            return;
        };
        let reply: redis::RedisResult<redis::Value> = redis::cmd("XRANGE")
            .arg(&self.config.stream)
            .arg("+")
            .arg("-")
            .arg("COUNT")
            .arg(1)
            .query_async(conn)
            .await;
        let entries = match reply {
            Ok(redis::Value::Array(entries)) => entries,
            Ok(_) => return,
            Err(_) => {
                self.conn = None;
                return;
            }
        };
        let Some(newest) = entries.first() else {
            // Empty/nonexistent stream: any stored cursor is from a previous
            // stream, so start at the current tail instead.
            self.cursor = "$".into();
            return;
        };
        let redis::Value::Array(parts) = newest else {
            return;
        };
        let Some(redis::Value::BulkString(id)) = parts.first() else {
            return;
        };
        let newest_id = String::from_utf8_lossy(id);
        if let (Some(cursor), Some(newest)) =
            (stream_id_key(&self.cursor), stream_id_key(&newest_id))
        {
            if newest < cursor {
                self.cursor = "$".into();
            }
        }
    }

    pub async fn next_batch(&mut self) -> Result<Vec<Event>, String> {
        // Reuse one connection across cycles; reconnect only after a failure.
        if self.conn.is_none() {
            self.conn = Some(
                self.client
                    .get_multiplexed_async_connection()
                    .await
                    .map_err(|e| e.to_string())?,
            );
        }
        let conn = self.conn.as_mut().expect("connection initialized above");
        let reply: redis::RedisResult<redis::Value> = redis::cmd("XREAD")
            .arg("COUNT")
            .arg(10)
            .arg("BLOCK")
            .arg(1000)
            .arg("STREAMS")
            .arg(&self.config.stream)
            .arg(&self.cursor)
            .query_async(conn)
            .await;
        if reply.is_err() {
            self.conn = None;
        }
        let events = parse_entries(reply)?;
        if let Some(last) = events.last() {
            self.cursor = last.id.clone();
        }
        Ok(events)
    }
}

fn parse_entries(reply: redis::RedisResult<redis::Value>) -> Result<Vec<Event>, String> {
    let value = reply.map_err(|e| e.to_string())?;
    let arrays = match value {
        redis::Value::Array(a) => a,
        redis::Value::Nil => return Ok(Vec::new()),
        _ => return Err("unexpected stream reply".into()),
    };
    // XREAD wraps replies as [[stream_name, [entry, ...]]] (one stream).
    // Unwrap that outer array, then the per-stream name/entries pair.
    let arrays = match arrays.as_slice() {
        [redis::Value::Array(inner)] => inner.clone(),
        _ => arrays,
    };
    let entries = match (arrays.first(), arrays.get(1)) {
        (Some(redis::Value::BulkString(_)), Some(redis::Value::Array(a))) => a.clone(),
        _ => arrays,
    };
    let mut out = Vec::new();
    for entry in entries {
        let parts = match entry {
            redis::Value::Array(a) => a,
            _ => continue,
        };
        let (id, fields) = match (parts.first(), parts.get(1)) {
            (Some(redis::Value::BulkString(b)), Some(redis::Value::Array(a))) => {
                (String::from_utf8_lossy(b).into_owned(), a.clone())
            }
            _ => continue,
        };
        let mut map = std::collections::HashMap::new();
        for pair in fields.chunks(2) {
            if pair.len() == 2 {
                let k = match &pair[0] {
                    redis::Value::BulkString(b) => String::from_utf8_lossy(b).into_owned(),
                    _ => continue,
                };
                let v = match &pair[1] {
                    redis::Value::BulkString(b) => String::from_utf8_lossy(b).into_owned(),
                    _ => continue,
                };
                map.insert(k, v);
            }
        }
        let payload: serde_json::Value =
            serde_json::from_str(map.get("payload").map(String::as_str).unwrap_or("{}"))
                .unwrap_or(serde_json::json!({}));
        out.push(Event {
            id,
            kind: map.get("kind").cloned().unwrap_or_default(),
            actor_sub: map.get("actor_sub").cloned().unwrap_or_default(),
            tenant: map.get("tenant").cloned().unwrap_or_default(),
            // Fail closed: an entry without visibility metadata must not be
            // broadcast to every authenticated viewer.
            visibility: visibility_from_str(
                map.get("visibility").map(String::as_str).unwrap_or("[]"),
            ),
            payload,
            created_at: map
                .get("created_at")
                .and_then(|v| v.parse().ok())
                .unwrap_or(0),
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn viewer(sub: &str) -> Viewer {
        Viewer {
            sub: sub.into(),
            email: format!("{sub}@example.invalid"),
            name: sub.into(),
            tenant: "t".into(),
        }
    }

    #[test]
    fn visibility_team_is_visible_to_everyone_and_recipients_only_to_members() {
        assert!(Visibility::Team.visible_to(&viewer("alice")));
        assert!(Visibility::Team.visible_to(&viewer("bob")));
        let v = Visibility::Recipients(vec!["alice".into(), "carol".into()]);
        assert!(v.visible_to(&viewer("alice")));
        assert!(v.visible_to(&viewer("carol")));
        assert!(!v.visible_to(&viewer("bob")));
    }

    #[test]
    fn recipients_match_store_identifiers_and_stay_off_unrelated_users() {
        // The stores identify people by email/name, the auth layer by `sub`.
        let recipient = Viewer {
            sub: "oidc-42".into(),
            email: "alice@example.invalid".into(),
            name: "Alice".into(),
            tenant: "t".into(),
        };
        let v = Visibility::Recipients(vec!["alice@example.invalid".into()]);
        assert!(v.visible_to(&recipient));
        assert!(!v.visible_to(&viewer("bob")));
        assert!(!Visibility::Recipients(vec!["oidc-42".into()]).visible_to(&viewer("bob")));
    }

    #[test]
    fn event_serializes_with_redis_id_as_sse_id_field() {
        let e = Event {
            id: "123-0".into(),
            kind: "intercom.created".into(),
            actor_sub: "alice".into(),
            tenant: "opensme.local".into(),
            visibility: Visibility::Team,
            payload: serde_json::json!({ "id": 7, "text": "hello" }),
            created_at: 1_700_000_000,
        };
        let json = serde_json::to_value(&e).unwrap();
        assert_eq!(json["id"], "123-0");
        assert_eq!(json["kind"], "intercom.created");
        assert_eq!(json["payload"]["id"], 7);
        assert!(
            json.get("visibility").is_none(),
            "visibility must not leak to the browser"
        );
    }

    #[test]
    fn sse_frame_contains_event_id_data_and_terminator() {
        let e = Event {
            id: "9-0".into(),
            kind: "task.created".into(),
            actor_sub: "a".into(),
            tenant: "t".into(),
            visibility: Visibility::Team,
            payload: serde_json::json!({ "id": 1 }),
            created_at: 0,
        };
        let frame = sse_frame(&e);
        assert!(frame.starts_with("event: task.created\nid: 9-0\ndata: "));
        assert!(frame.ends_with("\n\n"));
    }

    #[test]
    fn filter_drops_events_not_visible_to_viewer() {
        let team = Event {
            id: "1-0".into(),
            kind: "intercom.created".into(),
            actor_sub: "a".into(),
            tenant: "t".into(),
            visibility: Visibility::Team,
            payload: serde_json::json!({}),
            created_at: 0,
        };
        let private = Event {
            id: "2-0".into(),
            kind: "task.created".into(),
            actor_sub: "a".into(),
            tenant: "t".into(),
            visibility: Visibility::Recipients(vec!["a".into()]),
            payload: serde_json::json!({}),
            created_at: 0,
        };
        let seen = filter_visible(vec![team, private], &viewer("bob"));
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].id, "1-0");
    }

    #[test]
    fn filter_drops_events_from_another_tenant_even_when_team_visible() {
        let other = Event {
            id: "3-0".into(),
            kind: "intercom.created".into(),
            actor_sub: "a".into(),
            tenant: "other.example".into(),
            visibility: Visibility::Team,
            payload: serde_json::json!({}),
            created_at: 0,
        };
        assert!(filter_visible(vec![other], &viewer("bob")).is_empty());
    }

    #[test]
    fn malformed_cursor_resets_to_new_entries_only() {
        assert_eq!(sanitize_cursor(None), "$");
        assert_eq!(sanitize_cursor(Some("garbage".into())), "$");
        assert_eq!(sanitize_cursor(Some("12-3".into())), "12-3");
        assert_eq!(sanitize_cursor(Some("0-0".into())), "0-0");
    }

    #[test]
    fn stale_cursor_is_newer_than_the_current_stream_tail() {
        assert_eq!(stream_id_key("bad"), None);
        assert!(
            stream_id_key("12345-0").unwrap() > stream_id_key("42-9").unwrap(),
            "a cursor from a recreated stream must be detectable as ahead"
        );
    }

    #[tokio::test]
    async fn memory_bus_publishes_and_replays_in_order() {
        let bus = EventBus::memory();
        let a = Event::new(
            "intercom.created",
            "a",
            "t",
            Visibility::Team,
            serde_json::json!({"id": 1}),
        );
        let b = Event::new(
            "task.created",
            "a",
            "t",
            Visibility::Recipients(vec!["a".into()]),
            serde_json::json!({"id": 2}),
        );
        let id_a = bus.publish(&a).await.unwrap();
        let id_b = bus.publish(&b).await.unwrap();
        assert_eq!(id_a, "m-1");
        assert_eq!(id_b, "m-2");
        let all = bus.replay(None).await.unwrap();
        assert_eq!(all.len(), 2);
        assert_eq!(all[0].payload["id"], 1);
    }

    #[tokio::test]
    async fn disabled_bus_is_a_noop() {
        let bus = EventBus::disabled();
        assert!(bus
            .publish(&Event::new(
                "x",
                "a",
                "t",
                Visibility::Team,
                serde_json::json!({})
            ))
            .await
            .is_none());
        assert!(bus.replay(None).await.unwrap().is_empty());
    }

    #[test]
    fn stream_config_from_env_defaults_and_disables_on_empty_flag() {
        let cfg = StreamConfig::from_env(|_| None);
        assert!(!cfg.enabled);
        assert_eq!(cfg.stream, DEFAULT_STREAM);
        assert_eq!(cfg.maxlen, DEFAULT_MAXLEN);
    }

    #[test]
    fn stream_config_enables_when_flag_is_1_or_true() {
        for flag in ["1", "true", "TRUE"] {
            let cfg = StreamConfig::from_env(|k| match k {
                "EVENTS_ENABLED" => Some(flag.into()),
                "EVENTS_STREAM" => Some("custom".into()),
                "EVENTS_MAXLEN" => Some("50".into()),
                _ => None,
            });
            assert!(cfg.enabled, "flag {flag} should enable events");
            assert_eq!(cfg.stream, "custom");
            assert_eq!(cfg.maxlen, 50);
        }
    }

    fn bulk(s: &str) -> redis::Value {
        redis::Value::BulkString(s.as_bytes().to_vec())
    }

    #[test]
    fn parse_entries_reads_a_synthetic_xread_reply() {
        // XREAD returns [[stream_name, [[id, [field, value, ...]], ...]]].
        let reply = Ok(redis::Value::Array(vec![redis::Value::Array(vec![
            bulk(DEFAULT_STREAM),
            redis::Value::Array(vec![redis::Value::Array(vec![
                bulk("1-0"),
                redis::Value::Array(vec![
                    bulk("kind"),
                    bulk("intercom.created"),
                    bulk("payload"),
                    bulk(r#"{"id":1}"#),
                ]),
            ])]),
        ])]));
        let events = parse_entries(reply).expect("XREAD reply parses");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].id, "1-0");
        assert_eq!(events[0].kind, "intercom.created");
        assert_eq!(events[0].payload["id"], 1);
        // Missing visibility metadata fails closed: nobody may see it.
        assert!(filter_visible(events, &viewer("bob")).is_empty());
    }

    #[test]
    fn parse_entries_treats_nil_as_an_empty_block_timeout() {
        assert!(parse_entries(Ok(redis::Value::Nil))
            .expect("Nil reply is not an error")
            .is_empty());
    }
}
