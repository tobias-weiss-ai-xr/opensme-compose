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

impl Visibility {
    pub fn visible_to(&self, sub: &str) -> bool {
        match self {
            Visibility::Team => true,
            Visibility::Recipients(ids) => ids.iter().any(|id| id == sub),
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
pub fn filter_visible(events: Vec<Event>, viewer_sub: &str) -> Vec<Event> {
    events
        .into_iter()
        .filter(|e| e.visibility.visible_to(viewer_sub))
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
            enabled: get("EVENTS_ENABLED").as_deref() == Some("1"),
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
                let mut conn = match client.get_async_connection().await {
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
        viewer_sub: String,
        last_id: Option<String>,
    ) -> axum::response::Response {
        match &self.0 {
            BusInner::Disabled => StatusCode::NO_CONTENT.into_response(),
            BusInner::Memory { .. } => {
                // In-process contract tests can exercise replay; production uses Redis.
                let events = self.replay(None).await.unwrap_or_default();
                let body = filter_visible(events, &viewer_sub)
                    .iter()
                    .map(sse_frame)
                    .collect::<Vec<_>>()
                    .join("");
                ([(header::CONTENT_TYPE, "text/event-stream")], body).into_response()
            }
            BusInner::Redis { client, config } => {
                stream_events(client.clone(), config.clone(), viewer_sub, last_id)
                    .await
                    .into_response()
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

/// Minimal SSE reader over the Redis Stream for one browser connection.
/// Each connection owns its XREAD cursor (Last-Event-ID on reconnect, `$` on
/// first connect). No consumer group: a group would load-balance entries
/// across viewers, dropping events for all but one connection.
pub async fn stream_events(
    client: redis::Client,
    config: StreamConfig,
    viewer_sub: String,
    last_id: Option<String>,
) -> impl IntoResponse {
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<String, std::io::Error>>(16);
    let mut reader = EventReader {
        client,
        config,
        cursor: last_id.unwrap_or_else(|| "$".into()),
    };
    tokio::spawn(async move {
        loop {
            match reader.next_batch().await {
                Ok(events) => {
                    for event in filter_visible(events, &viewer_sub) {
                        if tx.send(Ok(sse_frame(&event))).await.is_err() {
                            return;
                        }
                    }
                }
                Err(e) => tracing::warn!(error = %e, "events: stream read failed"),
            }
            if tx.send(Ok(": keep-alive\n\n".into())).await.is_err() {
                return;
            }
            tokio::time::sleep(std::time::Duration::from_secs(15)).await;
        }
    });
    let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
    (
        [
            (header::CONTENT_TYPE, "text/event-stream"),
            (header::CACHE_CONTROL, "no-cache, no-transform"),
            (header::CONNECTION, "keep-alive"),
        ],
        Body::from_stream(stream),
    )
        .into_response()
}

pub struct EventReader {
    client: redis::Client,
    config: StreamConfig,
    cursor: String, // Redis stream ID, or "$" for new entries only
}

impl EventReader {
    pub async fn next_batch(&mut self) -> Result<Vec<Event>, String> {
        let mut conn = self
            .client
            .get_async_connection()
            .await
            .map_err(|e| e.to_string())?;
        let reply: redis::RedisResult<redis::Value> = redis::cmd("XREAD")
            .arg("COUNT")
            .arg(10)
            .arg("BLOCK")
            .arg(1000)
            .arg("STREAMS")
            .arg(&self.config.stream)
            .arg(&self.cursor)
            .query_async(&mut conn)
            .await;
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
            visibility: visibility_from_str(
                map.get("visibility").map(String::as_str).unwrap_or("team"),
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

    #[test]
    fn visibility_team_is_visible_to_everyone_and_recipients_only_to_members() {
        assert!(Visibility::Team.visible_to("alice"));
        assert!(Visibility::Team.visible_to("bob"));
        let v = Visibility::Recipients(vec!["alice".into(), "carol".into()]);
        assert!(v.visible_to("alice"));
        assert!(v.visible_to("carol"));
        assert!(!v.visible_to("bob"));
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
        let seen = filter_visible(vec![team, private], "bob");
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].id, "1-0");
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
    fn stream_config_enables_when_flag_is_1() {
        let cfg = StreamConfig::from_env(|k| match k {
            "EVENTS_ENABLED" => Some("1".into()),
            "EVENTS_STREAM" => Some("custom".into()),
            "EVENTS_MAXLEN" => Some("50".into()),
            _ => None,
        });
        assert!(cfg.enabled);
        assert_eq!(cfg.stream, "custom");
        assert_eq!(cfg.maxlen, 50);
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
    }

    #[test]
    fn parse_entries_treats_nil_as_an_empty_block_timeout() {
        assert!(parse_entries(Ok(redis::Value::Nil))
            .expect("Nil reply is not an error")
            .is_empty());
    }
}
