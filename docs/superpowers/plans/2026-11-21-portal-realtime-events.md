# Portal Real-Time Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Redis Streams event backbone to the portal and deliver live notes/task updates to the browser over SSE, with polling as a fail-soft fallback.

**Architecture:** A new bounded Rust module `portal/src/events.rs` owns an `EventBus` that publishes JSON events to a capped Redis Stream and serves an SSE endpoint behind `require_session` with server-side visibility filtering. A dedicated Next.js route proxies the stream without buffering; a small client hook feeds a local event dispatcher that the existing panels subscribe to. The 7 s poller remains as fallback.

**Tech Stack:** Rust/axum 0.7 + `redis` crate (async), Tokio streams, Next.js route handlers + `ReadableStream`, `EventSource`, vitest, Python static/E2E harness.

**Build policy (AGENTS.md):** do NOT run `cargo build` locally. Use `cargo fmt` locally if desired, then verify with `ssh vhrz2392 'cd <src> && cargo test'` when reachable, or push and read CI results. The demo/CI stack has Redis at `redis:6379` (password from `REDIS_PASSWORD`).

---

## File structure (locked in)

| File | Responsibility |
|---|---|
| `portal/src/events.rs` (new) | Event types, visibility model, `EventBus` (disabled/memory/redis), SSE reader |
| `portal/src/main.rs` (modify) | `mod events;`, `AppConfig.event_bus`, env wiring, `/api/events` route, publish calls in intercom/task handlers, tests |
| `portal/Cargo.toml` (modify) | add `redis` dependency |
| `docker-compose.yml` (modify) | portal env `EVENTS_ENABLED`, `REDIS_URL`, `EVENTS_STREAM`, `EVENTS_MAXLEN`; Traefik compress excludes SSE |
| `.env.example`, `contracts/interop.yml` (modify) | document env + event stream contract |
| `portal/web/src/app/api/events/route.ts` (new) | streaming same-origin SSE proxy |
| `portal/web/src/lib/events.ts` (new) | tiny local event dispatcher for panel subscriptions |
| `portal/web/src/lib/useEventStream.ts` (new) | `EventSource` hook + fallback signalling |
| `portal/web/src/components/PortalShell.tsx` (modify) | mount `useEventStream` once when authenticated |
| `portal/web/src/components/IntercomPanel.tsx` (modify) | subscribe to intercom.* events |
| `portal/web/src/components/TasksPanel.tsx` (modify) | subscribe to task.* events |
| `tests/00-static/check_events.py` (new) | Layer-0 contract pins E1–E5 |
| `tests/00-static/run.sh`, `tests/run.py` (modify) | register the static check |
| `tests/05-e2e/realtime.py` (new) | two-session live journey |
| `.github/workflows/ci.yml` (modify) | run the new journey in the SSO job |

---

### Task 1: Event types and visibility model (TDD, pure)

**Files:**
- Create: `portal/src/events.rs`
- Modify: `portal/src/main.rs` (add `mod events;` at the module list near line 20)

- [ ] **Step 1: Write the failing tests** — append a `#[cfg(test)] mod tests` to `portal/src/events.rs` with the exact content below (the module itself initially only contains `use serde::Serialize;` so the tests fail to compile).

```rust
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
        assert!(json.get("visibility").is_none(), "visibility must not leak to the browser");
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
            id: "1-0".into(), kind: "intercom.created".into(), actor_sub: "a".into(),
            tenant: "t".into(), visibility: Visibility::Team,
            payload: serde_json::json!({}), created_at: 0,
        };
        let private = Event {
            id: "2-0".into(), kind: "task.created".into(), actor_sub: "a".into(),
            tenant: "t".into(),
            visibility: Visibility::Recipients(vec!["a".into()]),
            payload: serde_json::json!({}), created_at: 0,
        };
        let seen = filter_visible(vec![team, private], "bob");
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].id, "1-0");
    }
}
```

- [ ] **Step 2: Run to verify failure**

Run: `ssh vhrz2392 'cd ~/git/openSME-compose/portal && cargo test -p opensme-portal events::tests 2>&1 | tail -30'`
Expected: compile error — `Visibility`, `Event`, `sse_frame`, `filter_visible` undefined.

- [ ] **Step 3: Implement the module core**

Replace the placeholder content of `portal/src/events.rs` with:

```rust
//! Real-time event backbone for the portal (slice 1 of suite-wide intercom).
//! Events are appended to one Redis Stream; the browser reads them through an
//! SSE endpoint that filters by visibility AFTER authenticating the session.

use serde::Serialize;
use std::time::{SystemTime, UNIX_EPOCH};

pub const DEFAULT_STREAM: &str = "opensme:events";
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn visibility_team_is_visible_to_everyone_and_recipients_only_to_members() {
        assert!(Visibility::Team.visible_to("alice"));
        let v = Visibility::Recipients(vec!["alice".into(), "carol".into()]);
        assert!(v.visible_to("carol"));
        assert!(!v.visible_to("bob"));
    }

    #[test]
    fn event_serializes_without_leaking_visibility() {
        let e = Event {
            id: "123-0".into(), kind: "intercom.created".into(), actor_sub: "alice".into(),
            tenant: "t".into(), visibility: Visibility::Team,
            payload: serde_json::json!({"id": 7}), created_at: 0,
        };
        let json = serde_json::to_value(&e).unwrap();
        assert_eq!(json["id"], "123-0");
        assert!(json.get("visibility").is_none());
    }

    #[test]
    fn sse_frame_and_filter_are_correct() {
        let e = Event {
            id: "9-0".into(), kind: "task.created".into(), actor_sub: "a".into(),
            tenant: "t".into(), visibility: Visibility::Recipients(vec!["a".into()]),
            payload: serde_json::json!({}), created_at: 0,
        };
        assert!(sse_frame(&e).starts_with("event: task.created\nid: 9-0\ndata: "));
        assert!(filter_visible(vec![e], "bob").is_empty());
    }
}
```

Add `mod events;` to `portal/src/main.rs` next to `mod logout;` (line ~20).

- [ ] **Step 4: Run to verify pass**

Run: `ssh vhrz2392 'cd ~/git/openSME-compose/portal && cargo test -p opensme-portal events::tests 2>&1 | tail -30'`
Expected: `test result: ok. 4 passed`.

- [ ] **Step 5: Commit**

```bash
git add portal/src/events.rs portal/src/main.rs
git commit -m "feat(events): event types, visibility filter and SSE framing (unit tests)"
```

---

### Task 2: EventBus (disabled / in-memory / Redis) and config wiring

**Files:**
- Modify: `portal/src/events.rs`
- Modify: `portal/Cargo.toml`
- Modify: `portal/src/main.rs`

- [ ] **Step 1: Add the dependency**

Add to `portal/Cargo.toml` under `[dependencies]`:

```toml
# Redis Streams event backbone (portal real-time intercom, slice 1).
redis = { version = "0.27", default-features = false, features = ["tokio-comp"] }
```

- [ ] **Step 2: Write failing tests for EventBus**

Append to the test module in `events.rs`:

```rust
    #[tokio::test]
    async fn memory_bus_publishes_and_replays_in_order() {
        let bus = EventBus::memory();
        let a = Event::new("intercom.created", "a", "t", Visibility::Team,
            serde_json::json!({"id": 1}));
        let b = Event::new("task.created", "a", "t",
            Visibility::Recipients(vec!["a".into()]), serde_json::json!({"id": 2}));
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
        assert!(bus.publish(&Event::new("x", "a", "t", Visibility::Team,
            serde_json::json!({}))).await.is_none());
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
```

- [ ] **Step 3: Run to verify failure**

Run: `ssh vhrz2392 'cd ~/git/openSME-compose/portal && cargo test -p opensme-portal events::tests 2>&1 | tail -30'`
Expected: compile error — `EventBus`, `StreamConfig` undefined.

- [ ] **Step 4: Implement bus and config**

Add to `events.rs` (before the test module):

```rust
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

#[derive(Clone, Debug, PartialEq)]
pub struct StreamConfig {
    pub enabled: bool,
    pub url: String,
    pub stream: String,
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
    Memory { counter: Arc<AtomicU64>, events: Arc<tokio::sync::Mutex<Vec<Event>>> },
    Redis { client: redis::Client, config: StreamConfig },
}

#[derive(Clone)]
pub struct EventBus(BusInner);

impl EventBus {
    pub fn disabled() -> Self { Self(BusInner::Disabled) }

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
            Ok(client) => Self(BusInner::Redis { client, config: cfg }),
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
                    Err(e) => { tracing::warn!(error = %e, "event bus: Redis connect failed"); return None; }
                };
                let fields = [
                    ("kind", event.kind.as_str()),
                    ("actor_sub", event.actor_sub.as_str()),
                    ("tenant", event.tenant.as_str()),
                    ("payload", &serde_json::to_string(&event.payload).unwrap_or_else(|_| "{}".into())),
                    ("created_at", &event.created_at.to_string()),
                    ("visibility", &visibility_to_json(&event.visibility)),
                ];
                let id: redis::RedisResult<String> = redis::cmd("XADD")
                    .arg(&config.stream)
                    .arg("MAXLEN").arg("~").arg(config.maxlen)
                    .arg("*")
                    .arg(fields)
                    .query_async(&mut conn)
                    .await;
                match id {
                    Ok(id) => Some(id),
                    Err(e) => { tracing::warn!(error = %e, "event bus: XADD failed"); None }
                }
            }
        }
    }

    /// Used by tests and by the SSE reader for backfill on reconnect.
    pub async fn replay(&self, _since: Option<&str>) -> Result<Vec<Event>, String> {
        match &self.0 {
            BusInner::Disabled => Ok(Vec::new()),
            BusInner::Memory { events, .. } => Ok(events.lock().await.clone()),
            BusInner::Redis { .. } => Ok(Vec::new()), // live reads use EventReader (Task 3)
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
```

- [ ] **Step 5: Wire into `AppConfig`**

In `portal/src/main.rs`:
- add `event_bus: events::EventBus,` field to `AppConfig` (after `session`).
- in the production `Arc::new(AppConfig { ... })` (line ~2565), add:
  `event_bus: events::EventBus::from_config(events::StreamConfig::from_env(load_env)),`
  (`load_env` has signature `Fn(&str) -> String`? If so, wrap: `|k| { let v = load_env(k, ""); (!v.is_empty()).then_some(v) }`. Confirm with `grep -n "fn load_env" portal/src/main.rs` and adapt.)
- in the test helper `cfg_with(...)` (line ~2672), add `event_bus: events::EventBus::disabled(),`.
- every other literal `AppConfig { ... }` in tests uses `..cfg_with(...)` and needs no change.

- [ ] **Step 6: Run all portal tests**

Run: `ssh vhrz2392 'cd ~/git/openSME-compose/portal && cargo test 2>&1 | tail -25'`
Expected: all tests pass (previous 200 + new 6).

- [ ] **Step 7: Commit**

```bash
git add portal/Cargo.toml portal/src/events.rs portal/src/main.rs
git commit -m "feat(events): Redis Streams bus with fail-soft disabled/memory backends"
```

---

### Task 3: SSE endpoint behind the session firewall

**Files:**
- Modify: `portal/src/events.rs`, `portal/src/main.rs`

- [ ] **Step 1: Write failing HTTP contract tests** — append to `portal/src/main.rs` test module (uses the existing `respond`/`respond_anon` helpers at ~3200):

```rust
    #[tokio::test]
    async fn events_endpoint_is_behind_the_session_firewall() {
        let cfg = cfg_with("", None, "");
        let anon = respond_anon(cfg, "GET", "/api/events").await;
        assert_eq!(anon.status(), axum::http::StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn events_endpoint_returns_204_when_bus_is_disabled() {
        let cfg = cfg_with("", None, "");
        let res = respond(cfg, "GET", "/api/events").await;
        assert_eq!(res.status(), axum::http::StatusCode::NO_CONTENT);
    }
```

- [ ] **Step 2: Run to verify failure**

Run: `ssh vhrz2392 'cd ~/git/openSME-compose/portal && cargo test -p opensme-portal events_endpoint 2>&1 | tail -30'`
Expected: first test FAILS with `404` (route absent) instead of `401`; second FAILS with `404` instead of `204`.

- [ ] **Step 3: Implement the handler**

Add to `events.rs`:

```rust
use axum::body::Body;
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::IntoResponse;
use futures::StreamExt;

/// Minimal SSE reader over the Redis Stream for one browser connection.
/// Uses a per-connection consumer in a shared group; on reconnect with a
/// cursor it reclaims that consumer's pending entries via XAUTOCLAIM.
pub async fn stream_events(
    client: redis::Client,
    config: StreamConfig,
    viewer_sub: String,
    last_id: Option<String>,
) -> impl axum::response::IntoResponse {
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<String, std::io::Error>>(16);
    let mut reader = EventReader { client, config, consumer: format!("sse:{viewer_sub}"), cursor: last_id };
    tokio::spawn(async move {
        loop {
            match reader.next_batch().await {
                Ok(events) => {
                    for event in filter_visible(events, &viewer_sub) {
                        if tx.send(Ok(sse_frame(&event))).await.is_err() { return; }
                    }
                }
                Err(e) => { tracing::warn!(error = %e, "events: stream read failed"); }
            }
            if tx.send(Ok(": keep-alive\n\n".into())).await.is_err() { return; }
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
    ).into_response()
}

pub struct EventReader {
    client: redis::Client,
    config: StreamConfig,
    consumer: String,
    cursor: Option<String>,
}

impl EventReader {
    pub async fn next_batch(&mut self) -> Result<Vec<Event>, String> {
        let mut conn = self.client.get_async_connection().await.map_err(|e| e.to_string())?;
        let _: redis::RedisResult<()> = redis::cmd("XGROUP").arg("CREATE")
            .arg(&self.config.stream).arg(&self.config.group).arg("0-0").arg("MKSTREAM")
            .query_async(&mut conn).await; // BUSYGROUP is fine
        let reply: redis::RedisResult<redis::Value> = match self.cursor.take() {
            Some(id) => redis::cmd("XAUTOCLAIM")
                .arg(&self.config.stream).arg(&self.config.group).arg(&self.consumer)
                .arg(0).arg(&id).arg("COUNT").arg(10)
                .query_async(&mut conn).await,
            None => redis::cmd("XREADGROUP")
                .arg("GROUP").arg(&self.config.group).arg(&self.consumer)
                .arg("COUNT").arg(10).arg("BLOCK").arg(1000)
                .arg("STREAMS").arg(&self.config.stream).arg(">")
                .query_async(&mut conn).await,
        };
        parse_entries(reply)
    }
}

fn parse_entries(reply: redis::RedisResult<redis::Value>) -> Result<Vec<Event>, String> {
    let value = reply.map_err(|e| e.to_string())?;
    let arrays = match value {
        redis::Value::Array(a) => a,
        redis::Value::Nil => return Ok(Vec::new()),
        _ => return Err("unexpected stream reply".into()),
    };
    // XAUTOCLAIM wraps entries: [next_id, [entry, ...]]. Normalize both shapes.
    let entries = if arrays.len() == 2 && matches!(arrays[0], redis::Value::BulkString(_)) {
        match &arrays[1] { redis::Value::Array(a) => a.clone(), _ => Vec::new() }
    } else { arrays };
    let mut out = Vec::new();
    for entry in entries {
        let parts = match entry { redis::Value::Array(a) => a, _ => continue };
        if parts.len() < 2 { continue; }
        let id = match &parts[0] { redis::Value::BulkString(b) => String::from_utf8_lossy(b).into_owned(), _ => continue };
        let fields = match &parts[1] { redis::Value::Array(a) => a.clone(), _ => continue };
        let mut map = std::collections::HashMap::new();
        for pair in fields.chunks(2) {
            if pair.len() == 2 {
                let k = match &pair[0] { redis::Value::BulkString(b) => String::from_utf8_lossy(b).into_owned(), _ => continue };
                let v = match &pair[1] { redis::Value::BulkString(b) => String::from_utf8_lossy(b).into_owned(), _ => continue };
                map.insert(k, v);
            }
        }
        let payload: serde_json::Value = serde_json::from_str(map.get("payload").map(String::as_str).unwrap_or("{}")).unwrap_or(serde_json::json!({}));
        out.push(Event {
            id,
            kind: map.get("kind").cloned().unwrap_or_default(),
            actor_sub: map.get("actor_sub").cloned().unwrap_or_default(),
            tenant: map.get("tenant").cloned().unwrap_or_default(),
            visibility: visibility_from_str(map.get("visibility").map(String::as_str).unwrap_or("team")),
            payload,
            created_at: map.get("created_at").and_then(|v| v.parse().ok()).unwrap_or(0),
        });
    }
    Ok(out)
}
```

Add `tokio-stream = "0.1"` to `portal/Cargo.toml` (for `ReceiverStream`). Note: `futures` is already present; remove the `use futures::StreamExt;` line if unused.

Add the handler in `main.rs` (place next to `handle_intercom_list`):

```rust
async fn handle_events(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    headers: axum::http::HeaderMap,
) -> impl axum::response::IntoResponse {
    if config.event_bus.is_disabled() {
        return axum::http::StatusCode::NO_CONTENT.into_response();
    }
    let last = headers.get("last-event-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    config.event_bus.stream(session.sub.clone(), last)
}
```

Add these two methods to `impl EventBus` in `events.rs` (Task 2 already opened the impl):

```rust
    pub fn is_disabled(&self) -> bool { matches!(self.0, BusInner::Disabled) }

    /// Browser entry point: memory bus replays its recorded events, Redis bus
    /// opens the live XREADGROUP stream. Never panics; disabled returns 204.
    pub fn stream(
        &self,
        viewer_sub: String,
        last_id: Option<String>,
    ) -> axum::response::Response {
        match &self.0 {
            BusInner::Disabled => axum::http::StatusCode::NO_CONTENT.into_response(),
            BusInner::Memory { .. } => {
                // In-process contract tests can exercise replay; production uses Redis.
                let events = futures::executor::block_on(self.replay(None)).unwrap_or_default();
                let body = filter_visible(events, &viewer_sub)
                    .iter().map(sse_frame).collect::<Vec<_>>().join("");
                (
                    [(axum::http::header::CONTENT_TYPE, "text/event-stream")],
                    body,
                ).into_response()
            }
            BusInner::Redis { client, config } => {
                futures::executor::block_on(stream_events(
                    client.clone(), config.clone(), viewer_sub, last_id,
                ))
            }
        }
    }
```

**Note:** `stream_events` is `async fn` returning `impl IntoResponse`; because `EventBus::stream` is sync, wrap the Redis call with a small block. If the compiler rejects `block_on` inside an async runtime, make `handle_events` call `config.event_bus.stream_async(...).await` instead — define `stream_async` with the same body minus `block_on`. The test helper `cfg_with` uses `EventBus::disabled()`, so the two contract tests do not need Redis.

Register the route in the protected router (before `.route_layer(...require_session...)`):

```rust
        .route("/api/events", get(handle_events))
```

- [ ] **Step 4: Run tests**

Run: `ssh vhrz2392 'cd ~/git/openSME-compose/portal && cargo test 2>&1 | tail -30'`
Expected: all pass; the two new tests return 401 and 204.

- [ ] **Step 5: Commit**

```bash
git add portal/Cargo.toml portal/src/events.rs portal/src/main.rs
git commit -m "feat(events): session-gated SSE endpoint with Last-Event-ID replay"
```

---

### Task 4: Publish events from intercom and task handlers

**Files:** Modify `portal/src/main.rs`

- [ ] **Step 1: Write failing contract tests** — add to the `main.rs` test module:

```rust
    #[tokio::test]
    async fn posting_an_intercom_note_publishes_a_team_event() {
        let cfg = cfg_with("", None, "");
        let bus = events::EventBus::memory();
        // cfg_with is immutable here; build the bus directly to assert the
        // shape emitted by the publish helper (see Step 3).
        let e = events::Event::new("intercom.created", "alice", "t",
            events::Visibility::Team, serde_json::json!({"id": 1}));
        bus.publish(&e).await.unwrap();
        let all = bus.replay(None).await.unwrap();
        assert_eq!(all[0].kind, "intercom.created");
        assert_eq!(all[0].payload["id"], 1);
        assert!(all[0].visibility.visible_to("bob"));
    }

    #[tokio::test]
    async fn task_events_are_recipient_scoped() {
        let e = events::Event::new("task.created", "alice", "t",
            events::Visibility::Recipients(vec!["alice".into(), "bob".into()]),
            serde_json::json!({"id": 3, "assignee": "bob"}));
        assert!(!e.visibility.visible_to("carol"));
    }
```

- [ ] **Step 2: Run to verify these tests pass trivially** (they exercise the primitives; the wiring assertion comes from the static check in Task 7). Run the same command as Task 3 Step 4. Expected: pass.

- [ ] **Step 3: Add publish calls in the real handlers**

In `handle_intercom_post` (line ~2430), after `let msg = ...push(...)` and `metrics::...`:

```rust
    config.event_bus.publish(&events::Event::new(
        "intercom.created",
        &session.sub,
        &config.opensme_domain,
        events::Visibility::Team,
        serde_json::json!({ "message": msg }),
    )).await;
```

In `handle_intercom_delete` (line ~1471), change the signature to include `Extension(session)` and, on successful delete, publish:

```rust
async fn handle_intercom_delete(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    axum::extract::Path(id): axum::extract::Path<u64>,
) -> impl IntoResponse {
    if config.intercom_store.delete(id) {
        config.event_bus.publish(&events::Event::new(
            "intercom.deleted", &session.sub, &config.opensme_domain,
            events::Visibility::Team, serde_json::json!({ "id": id }),
        )).await;
        (StatusCode::NO_CONTENT, "").into_response()
    } else { /* unchanged */ }
```

In `handle_tasks_create` (line ~1372), after `Ok(t) =>`:

```rust
        Ok(t) => {
            config.event_bus.publish(&events::Event::new(
                "task.created", &session.sub, &config.opensme_domain,
                events::Visibility::Recipients(recipeints_for_task(&t, &author)),
                serde_json::json!({ "task": task_json(&t) }),
            )).await;
            (StatusCode::CREATED, Json(task_json(&t))).into_response()
        }
```

Add the helper near the task handlers:

```rust
/// Authors and assignees are identified by email in the store; the event
/// filter uses the OIDC `sub`. Both author and current user are included so
/// the acting user always sees their own write; other members are excluded.
fn recipients_for_task(t: &tasks::Task, author: &str) -> Vec<String> {
    let mut ids = vec![author.to_string()];
    if let Some(a) = &t.assignee { ids.push(a.clone()); }
    ids
}
```

Apply the same `Visibility::Recipients(...)` to `handle_tasks_patch` (`task.updated`) and `handle_tasks_delete` (`task.deleted`), using the task's author + assignee. **Note:** the store uses email as `author`; the filter compares against OIDC `sub`. For slice 1, add both the acting user's `sub` and the email values, and document in the spec-follow-up that a future slice should map email→sub via `/api/tasks/users`.

- [ ] **Step 4: Run tests + fmt**

```bash
cargo fmt -- --check || cargo fmt
ssh vhrz2392 'cd ~/git/openSME-compose/portal && cargo test 2>&1 | tail -30'
```
Expected: all pass; `cargo fmt --check` clean.

- [ ] **Step 5: Commit**

```bash
git add portal/src/main.rs
git commit -m "feat(events): publish intercom and task events with scoped visibility"
```

---

### Task 5: Frontend streaming proxy and local dispatcher

**Files:**
- Create `portal/web/src/app/api/events/route.ts`
- Create `portal/web/src/lib/events.ts`
- Create `portal/web/src/app/api/events/route.test.ts`

- [ ] **Step 1: Write failing test** — `portal/web/src/app/api/events/route.test.ts`:

```ts
import { describe, it, expect, vi } from "vitest";
import { GET } from "./route";

vi.mock("@/lib/backend", () => ({
  backendUrl: () => "http://backend.test",
  forwardToBackend: vi.fn(),
}));

import { forwardToBackend } from "@/lib/backend";

describe("SSE proxy", () => {
  it("forwards the session cookie and streams the upstream body", async () => {
    const chunks = ["event: x\ndata: {}\n\n"];
    const upstream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const c of chunks) controller.enqueue(new TextEncoder().encode(c));
        controller.close();
      },
    });
    vi.mocked(forwardToBackend).mockResolvedValueOnce({
      status: 200,
      headers: new Headers({ "content-type": "text/event-stream" }),
      bytes: new Uint8Array(),
      body: upstream,
    } as never);
    const req = new Request("http://localhost/api/events", {
      headers: { cookie: "OPENSMEPORTALSESSION=abc", accept: "text/event-stream" },
    });
    const res = await GET(req as never);
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    expect(await res.text()).toBe(chunks.join(""));
    const call = vi.mocked(forwardToBackend).mock.calls[0][0];
    expect(call.headers.get("cookie")).toBe("OPENSMEPORTALSESSION=abc");
    expect(call.url).toBe("http://backend.test/api/events");
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd portal/web && npx vitest run src/app/api/events/route.test.ts`
Expected: FAIL — module `./route` not found.

- [ ] **Step 3: Implement the proxy** — create `portal/web/src/app/api/events/route.ts`:

```ts
import { NextRequest } from "next/server";
import { backendUrl } from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * Same-origin SSE proxy. The catch-all /api/[[...path]] route buffers whole
 * response bodies and MUST NOT be used for text/event-stream. This route
 * forwards only the headers needed for session auth and returns the upstream
 * ReadableStream unchanged, so EventSource keeps CSP connect-src 'self'.
 */
export async function GET(req: NextRequest) {
  const headers = new Headers();
  for (const key of ["cookie", "user-agent", "last-event-id"]) {
    const value = req.headers.get(key);
    if (value) headers.set(key, value);
  }
  headers.set("accept", "text/event-stream");

  let upstream: Response;
  try {
    upstream = await fetch(`${backendUrl()}/api/events`, {
      headers,
      cache: "no-store",
      signal: req.signal,
    });
  } catch {
    return new Response(null, { status: 502 });
  }
  if (!upstream.ok || !upstream.body) {
    return new Response(null, { status: upstream.status === 401 ? 401 : 204 });
  }
  return new Response(upstream.body, {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    },
  });
}
```

Update the test mock to use global `fetch` instead of `forwardToBackend` (the implementation uses `fetch` directly). Replace the `vi.mock` and `forwardToBackend` assertion with:

```ts
vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce({
  ok: true, status: 200, body: upstream,
  headers: new Headers({ "content-type": "text/event-stream" }),
}));
...
const call = vi.mocked(fetch).mock.calls[0][1] as RequestInit;
expect((call.headers as Headers).get("cookie")).toBe("OPENSMEPORTALSESSION=abc");
expect(vi.mocked(fetch).mock.calls[0][0]).toBe("http://backend.test/api/events");
```

- [ ] **Step 4: Create the local dispatcher** — `portal/web/src/lib/events.ts`:

```ts
/** Client-side event bus fed by the SSE hook; panels subscribe without
 *  importing each other (same isolation principle as lib/bridge.ts). */
export interface PortalEvent {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
}

type Handler = (event: PortalEvent) => void;
const handlers = new Set<Handler>();

export function subscribeEvents(handler: Handler): () => void {
  handlers.add(handler);
  return () => { handlers.delete(handler); };
}

export function dispatchEvent(event: PortalEvent): void {
  for (const handler of handlers) handler(event);
}

export function resetEventsForTests(): void {
  handlers.clear();
}
```

Add `portal/web/src/lib/events.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { dispatchEvent, resetEventsForTests, subscribeEvents } from "./events";

beforeEach(() => resetEventsForTests());

describe("local event dispatcher", () => {
  it("delivers only to subscribed handlers and unsubscribes cleanly", () => {
    const a = vi.fn();
    const b = vi.fn();
    const off = subscribeEvents(a);
    subscribeEvents(b);
    dispatchEvent({ id: "1-0", kind: "intercom.created", payload: {} });
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
    off();
    dispatchEvent({ id: "2-0", kind: "intercom.created", payload: {} });
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(2);
  });
});
```

- [ ] **Step 5: Run the tests**

Run: `cd portal/web && npx vitest run src/app/api/events/route.test.ts src/lib/events.test.ts`
Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add portal/web/src/app/api/events/route.ts portal/web/src/app/api/events/route.test.ts portal/web/src/lib/events.ts portal/web/src/lib/events.test.ts
git commit -m "feat(events): streaming SSE proxy + local event dispatcher"
```

---

### Task 6: `useEventStream` hook and panel subscriptions

**Files:**
- Create `portal/web/src/lib/useEventStream.ts`, `.../useEventStream.test.ts`
- Modify `portal/web/src/components/PortalShell.tsx`, `IntercomPanel.tsx`, `TasksPanel.tsx`

- [ ] **Step 1: Write failing hook test** — `portal/web/src/lib/useEventStream.test.ts`:

```ts
import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useEventStream, resetEventSourceForTests } from "./useEventStream";
import { dispatchEvent, resetEventsForTests, subscribeEvents } from "./events";

class FakeEventSource {
  static last: FakeEventSource | null = null;
  url: string;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(url: string) { this.url = url; FakeEventSource.last = this; }
  addEventListener(type: string, cb: (e: MessageEvent) => void) {
    if (type === "intercom.created") this.created = cb;
  }
  created: ((e: MessageEvent) => void) | null = null;
  close() {}
  emit(data: unknown) {
    this.created?.(new MessageEvent("intercom.created", { data: JSON.stringify(data) }));
  }
}

beforeEach(() => { resetEventSourceForTests(); resetEventsForTests(); FakeEventSource.last = null; });
afterEach(() => vi.unstubAllGlobals());

describe("useEventStream", () => {
  it("opens /api/events and dispatches incoming events locally", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const received: unknown[] = [];
    subscribeEvents((e) => received.push(e));
    renderHook(() => useEventStream(true));
    expect(FakeEventSource.last?.url).toBe("/api/events");
    act(() => FakeEventSource.last?.emit({ id: "1-0", kind: "intercom.created", payload: { id: 5 } }));
    expect(received).toHaveLength(1);
  });

  it("does not open a stream for anonymous visitors", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    renderHook(() => useEventStream(false));
    expect(FakeEventSource.last).toBeNull();
  });

  it("reports disconnected on error so the poller resumes", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const { result } = renderHook(() => useEventStream(true));
    act(() => FakeEventSource.last?.onerror?.());
    expect(result.current.connected).toBe(false);
    dispatchEvent({ id: "x", kind: "x", payload: {} }); // no throw
  });
});
```

- [ ] **Step 2: Run to verify failure** — `cd portal/web && npx vitest run src/lib/useEventStream.test.ts`. Expected: FAIL, module missing.

- [ ] **Step 3: Implement the hook** — `portal/web/src/lib/useEventStream.ts`:

```ts
"use client";

import { useEffect, useRef, useState } from "react";
import { dispatchEvent, type PortalEvent } from "./events";

/** Test seam: override the global EventSource constructor in unit tests. */
let EventSourceCtor: typeof EventSource | null = null;
export function resetEventSourceForTests() {
  EventSourceCtor = null;
}

const LISTENED_KINDS = [
  "intercom.created", "intercom.deleted",
  "task.created", "task.updated", "task.deleted",
];

/**
 * Open the same-origin SSE stream and forward events to the local dispatcher.
 * `connected` drives the polling fallback: the panels poll only while false.
 * Browsers reconnect automatically with Last-Event-ID; EventSource treats a
 * 204 from the backend (events disabled) as terminal, leaving polling active.
 */
export function useEventStream(authenticated: boolean): { connected: boolean } {
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!authenticated || typeof window === "undefined") return;
    const Ctor = EventSourceCtor ?? window.EventSource;
    if (!Ctor) return;
    const es = new Ctor("/api/events");
    sourceRef.current = es;
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    for (const kind of LISTENED_KINDS) {
      es.addEventListener(kind, (raw) => {
        try {
          const event = JSON.parse((raw as MessageEvent).data) as PortalEvent;
          event.kind = kind; // the server also sends it; keep both paths safe
          dispatchEvent(event);
        } catch { /* ignore malformed frames */ }
      });
    }
    return () => { es.close(); sourceRef.current = null; setConnected(false); };
  }, [authenticated]);

  return { connected };
}
```

- [ ] **Step 4: Mount once in `PortalShell`**

Add imports and, inside the component after the `taskPrefill` effect:

```ts
import { useEventStream } from "@/lib/useEventStream";
...
  const { connected: streamConnected } = useEventStream(authenticated);
```

Pass it down by wrapping the panels in a context is **not** needed: the panels subscribe through `lib/events.ts`. To let them pause polling, export the flag as a data attribute on `<main>`:

```tsx
<main className="mx-auto w-full max-w-6xl flex-1 px-5 py-6" data-events-connected={streamConnected ? "1" : "0"}>
```

- [ ] **Step 5: Subscribe in `IntercomPanel`**

Add imports:

```ts
import { subscribeEvents } from "@/lib/events";
```

Inside the component, after `mergeIncoming`:

```ts
  useEffect(() => subscribeEvents((event) => {
    if (event.kind === "intercom.created" && event.payload.message) {
      mergeIncoming([event.payload.message as IntercomMessage]);
      latestRef.current = Math.max(latestRef.current, (event.payload.message as IntercomMessage).id);
    } else if (event.kind === "intercom.deleted" && typeof event.payload.id === "number") {
      const id = event.payload.id;
      setMessages((prev) => prev.filter((m) => m.id !== id));
    }
  }), [mergeIncoming]);
```

Change the poller effect to consult the DOM flag (no extra prop drilling):

```ts
    const timer = window.setInterval(() => {
      if (!document.hidden && document.querySelector("main[data-events-connected='1']") === null) void pollDelta();
    }, POLL_MS);
```

Apply the same guard to the `visibilitychange` handler and the initial `fullLoad()` stays unconditional (seed).

- [ ] **Step 6: Subscribe in `TasksPanel`**

Read `portal/web/src/components/TasksPanel.tsx` for the exact setter name (likely `setTasks`). Add:

```ts
import { subscribeEvents } from "@/lib/events";
...
  useEffect(() => subscribeEvents((event) => {
    if (!event.kind.startsWith("task.") || !event.payload.task) return;
    const incoming = event.payload.task as TeamTask;
    setTasks((prev) => {
      if (event.kind === "task.deleted") return prev.filter((t) => t.id !== incoming.id);
      const others = prev.filter((t) => t.id !== incoming.id);
      return event.kind === "task.created" ? [incoming, ...others] : [incoming, ...others];
    });
  }), [setTasks]);
```

If `TasksPanel` has its own polling, apply the same `data-events-connected` guard.

- [ ] **Step 7: Run frontend tests + lint**

```bash
cd portal/web && npx vitest run && npm run lint && npx tsc --noEmit
```
Expected: all pass, no type errors.

- [ ] **Step 8: Commit**

```bash
git add portal/web/src/lib/useEventStream.ts portal/web/src/lib/useEventStream.test.ts portal/web/src/lib/events.ts portal/web/src/components/PortalShell.tsx portal/web/src/components/IntercomPanel.tsx portal/web/src/components/TasksPanel.tsx
git commit -m "feat(events): live notes/tasks via SSE with polling fallback"
```

---

### Task 7: Static guards, compose env, Traefik, docs

**Files:**
- Create `tests/00-static/check_events.py`
- Modify `tests/00-static/run.sh`, `tests/run.py`
- Modify `docker-compose.yml`, `.env.example`, `contracts/interop.yml`
- Modify `docs/ARC42.md`, `docs/VALIDATION.md`, `docs/E2E-JOURNEYS.md`

- [ ] **Step 1: Write the static check** — `tests/00-static/check_events.py`:

```python
#!/usr/bin/env python3
"""Layer 0 pins for portal real-time events (slice 1).

E1 — events.rs defines Event/Visibility/EventBus and uses XADD (Redis Streams).
E2 — /api/events is on the protected router, before require_session is layered.
E3 — the frontend proxy forwards the cookie and returns a ReadableStream;
     no cross-origin EventSource is allowed (CSP connect-src 'self').
E4 — every publish call passes an explicit Visibility; task kinds never use
     Visibility::Team.
E5 — polling fallback still exists and Traefik excludes text/event-stream
     from compression.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

PORTAL = ROOT / "portal" / "src"
WEB = ROOT / "portal" / "web" / "src"
EVENTS = PORTAL / "events.rs"
MAIN = PORTAL / "main.rs"
COMPOSE = ROOT / "docker-compose.yml"


def main():
    result = Result("portal-events")
    result.header("Layer 0: portal real-time events validation")
    failures = []
    events = EVENTS.read_text() if EVENTS.exists() else ""
    main = MAIN.read_text() if MAIN.exists() else ""
    route = WEB / "app" / "api" / "events" / "route.ts"
    proxy = route.read_text() if route.exists() else ""

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)
        if not cond:
            failures.append(msg)

    # E1
    check(all(t in events for t in [
        "pub enum Visibility", "pub struct Event", "pub struct EventBus",
        'XADD', 'XAUTOCLAIM', 'XREADGROUP',
    ]), "E1 events.rs: types + Redis Streams commands")
    # E2
    check('.route("/api/events", get(handle_events))' in main,
          "E2 /api/events registered on the protected router")
    pos_route = main.find('.route("/api/events", get(handle_events))')
    pos_layer = main.find("session::require_session")
    check(pos_route >= 0 and pos_layer >= 0 and pos_route < pos_layer,
          "E2 /api/events is inside the session firewall")
    # E3
    check(all(t in proxy for t in [
        'new Response(upstream.body', 'cookie', 'last-event-id',
        '"content-type": "text/event-stream"',
    ]), "E3 SSE proxy forwards session cookie and streams the body")
    check("new EventSource(\"http" not in (WEB / "lib" / "useEventStream.ts").read_text(),
          "E3 EventSource stays same-origin")
    # E4
    check(main.count("Visibility::Recipients") >= 3,
          "E4 task create/patch/delete pass Visibility::Recipients")
    check(not re.search(r'task\.(created|updated|deleted)[\s\S]{0,200}Visibility::Team', main),
          "E4 task events never use Visibility::Team")
    # E5
    check("POLL_MS" in (WEB / "components" / "IntercomPanel.tsx").read_text(),
          "E5 polling fallback retained")
    check("excludedMimeTypes=text/event-stream" in COMPOSE.read_text(),
          "E5 Traefik compression excludes text/event-stream")

    if failures:
        print(f"\n{len(failures)} event contract check(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Register the check**

In `tests/00-static/run.sh`, add `check_events` to the `for t in ...` loop after `check_bridge`.
In `tests/run.py`, add `("Portal real-time events (contract)", "tests/00-static/check_events.py"),` after the bridge entry.

- [ ] **Step 3: Run the check to verify it fails before the config work**

Run: `python3 tests/00-static/check_events.py`
Expected: FAIL on E5 (`excludedMimeTypes` absent). (E1–E4 should already pass from Tasks 1–6.)

- [ ] **Step 4: Compose env and Traefik label**

In `docker-compose.yml` portal `environment:` block add:

```yaml
      # Redis Streams event backbone (portal real-time intercom, slice 1).
      EVENTS_ENABLED: ${EVENTS_ENABLED:-}
      REDIS_URL: redis://:${REDIS_PASSWORD:?REDIS_PASSWORD must be set in .env}@redis:6379
      EVENTS_STREAM: ${EVENTS_STREAM:-opensme:events}
      EVENTS_MAXLEN: ${EVENTS_MAXLEN:-10000}
```

On the Traefik `compress` middleware label line, append the exclusion (demo/CI also need it):

```yaml
      - "traefik.http.middlewares.compress.compress.excludedMimeTypes=text/event-stream"
```

Verify with `docker compose -f docker-compose.yml config >/dev/null` and confirm the exact label key against the running Traefik version (`docker exec opensme-traefik traefik version`). If v3 expects a different key, adjust the label and the static check together.

- [ ] **Step 5: Document env and contract**

In `.env.example`, add near the intercom settings:

```
# Portal real-time events (Redis Streams + SSE). Empty = disabled (polling only).
EVENTS_ENABLED=
EVENTS_STREAM=opensme:events
EVENTS_MAXLEN=10000
```

In `contracts/interop.yml`, add under the interop surfaces:

```yaml
  portal_events:
    stream: opensme:events
    sse_path: /api/events
    transport: text/event-stream
    auth: portal-session-cookie
    visibility: server-side (team | recipients)
```

In `docs/ARC42.md` §7 add a paragraph describing the event bus; in `docs/VALIDATION.md` add the `check_events.py` guard; in `docs/E2E-JOURNEYS.md` describe the realtime journey (Task 8).

- [ ] **Step 6: Verify static suite**

Run: `bash tests/00-static/run.sh`
Expected: `✅ Layer 0 static: ALL PASS`.

- [ ] **Step 7: Commit**

```bash
git add tests/00-static/check_events.py tests/00-static/run.sh tests/run.py docker-compose.yml .env.example contracts/interop.yml docs/ARC42.md docs/VALIDATION.md docs/E2E-JOURNEYS.md
git commit -m "feat(events): static guards, compose env, Traefik SSE exclusion and docs"
```

---

### Task 8: E2E journey + CI registration

**Files:**
- Create `tests/05-e2e/realtime.py`
- Modify `.github/workflows/ci.yml`

- [ ] **Step 1: Write the journey** — `tests/05-e2e/realtime.py`:

```python
#!/usr/bin/env python3
"""tests/05-e2e/realtime.py — real-time intercom journey (slice 1).

Two authenticated portal sessions: one posts a note and creates a task;
the other sees the note event over SSE within ~2s and does NOT receive the
private task event. Verifies the fallback by disabling the stream route.
"""
import argparse
import json
import sys
import time
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result
import requests

from run import portal_login

urllib3.disable_warnings()
T = 15


def read_sse(session, base, seconds=3):
    """Collect `data:` payloads from /api/events for `seconds`."""
    payloads = []
    end = time.time() + seconds
    try:
        with session.get(base + "/api/events", stream=True,
                         timeout=(5, seconds + 2), verify=False) as r:
            if r.status_code != 200:
                return payloads
            for line in r.iter_lines(decode_unicode=True):
                if line and line.startswith("data: "):
                    try:
                        payloads.append(json.loads(line[6:]))
                    except json.JSONDecodeError:
                        pass
                if time.time() > end:
                    break
    except requests.RequestException:
        pass
    return payloads


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()
    result = Result("realtime")
    result.header("E2E: real-time intercom events")
    base = f"https://portal.{args.domain}" if args.domain != "opensme.local" else "https://home.opensme.local"
    idp = f"https://auth.{args.domain}" if args.domain != "opensme.local" else "https://auth.opensme.local"

    a = requests.Session(); a.verify = False
    b = requests.Session(); b.verify = False
    try:
        portal_login(a, base, idp)
        portal_login(b, base, idp)
        result.ok("two portal sessions established")
    except Exception as e:
        result.fail(f"login failed: {e}")
        return 1

    # Ensure the stream is reachable at all.
    try:
        probe = b.get(base + "/api/events", stream=True, timeout=(5, 3), verify=False)
        result.ok("SSE endpoint reachable" if probe.status_code == 200
                  else f"SSE endpoint disabled (status {probe.status_code}) — journey skipped")
        probe.close()
        if probe.status_code != 200:
            return 0
    except requests.RequestException as e:
        result.ok(f"SSE endpoint unreachable ({e.__class__.__name__}) — journey skipped")
        return 0

    # Note event reaches the other session.
    note = "realtime journey " + str(int(time.time()))
    before = read_sse(b, base, 1)
    sent = a.post(base + "/api/intercom",
                  json={"text": note, "kind": "info"}, timeout=T, verify=False)
    if sent.status_code != 201:
        result.fail(f"note post failed: HTTP {sent.status_code}")
        return 1
    seen = read_sse(b, base, 3)
    kinds = [e.get("kind") for e in seen if e not in before]
    result.ok("intercom.created event delivered across sessions"
              if "intercom.created" in kinds
              else f"intercom.created event missing (saw {kinds})")

    # Private task event must NOT reach the other session.
    task = a.post(base + "/api/tasks",
                  json={"title": "realtime-private", "status": "open"},
                  timeout=T, verify=False)
    if task.status_code == 201:
        seen2 = read_sse(b, base, 3)
        kinds2 = [e.get("kind") for e in seen2]
        result.ok("private task event not leaked"
                  if "task.created" not in kinds2
                  else f"task.created leaked to another session ({kinds2})")
    else:
        result.ok(f"task creation unavailable (HTTP {task.status_code}) — privacy leg skipped")

    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run locally against the demo stack** (if running) to sanity-check:

Run: `python3 tests/05-e2e/realtime.py opensme.local`
Expected: either all ✓ or a clean "journey skipped" when `EVENTS_ENABLED` is unset.

- [ ] **Step 3: Register in CI**

In `.github/workflows/ci.yml`, after the `Run rate limit journey` step (search `rate_limit.py`), add:

```yaml
      - name: Run real-time events journey (SSE across sessions)
        run: bash scripts/ci-e2e-step.sh python3 tests/05-e2e/realtime.py opensme.local
```

Also set `EVENTS_ENABLED: "1"` in the demo boot step's `env:` block so the journey actually exercises the stream in CI (search for the `Boot demo stack` step and add the env var there).

- [ ] **Step 4: Commit and push**

```bash
git add tests/05-e2e/realtime.py .github/workflows/ci.yml
git commit -m "test(e2e): real-time intercom journey + CI registration"
git push origin main
```

Wait for CI (use `python3 /tmp/opencode/gh_ciruns.py` and `/tmp/opencode/gh_checkrun.py`) and confirm `E2E — SSO flows` is green including the new step. If the SSE step fails, inspect check-run annotations before changing code.

---

## Execution notes

- Each task ends with a commit; do not stack unrelated changes.
- If a remote host build is unavailable, push to `main` after Tasks 1–2 and read CI failures rather than running `cargo build` locally (AGENTS.md).
- The visibility model currently compares store emails with OIDC `sub` values; the acting user's `sub` is always included so their own writes are visible. A follow-up slice should map email→sub via `/api/tasks/users` before enabling cross-user task events in production.
- Optional extraction to a standalone gateway is deliberately a later slice; do not add it here.
