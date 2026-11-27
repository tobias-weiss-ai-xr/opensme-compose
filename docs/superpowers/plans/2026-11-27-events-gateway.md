# Standalone Events Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, read-only SSE gateway crate that reuses the portal's event module and verifies portal session cookies itself.

**Architecture:** `gateway/` is a small axum binary using `#[path = "../../portal/src/events.rs"] mod events;`, so SSE framing, cursor sanitisation, XREVRANGE resume, visibility filtering and stream limits stay identical. A local session module verifies `OPENSMEPORTALSESSION` with the same HMAC format as the portal. Compose exposes the service only under the `events-gateway` profile.

**Tech Stack:** Rust/axum 0.7, Redis Streams via the shared events module, Docker Compose, Layer-0 Python static guards.

---

### Task 1: RED Layer-0 guard

**Files:**
- Create: `tests/00-static/check_events_gateway.py`
- Modify: `tests/run.py`
- Modify: `tests/00-static/check_mutations.py`
- Modify: `docs/VALIDATION.md`

- [x] **Step 1: Write the guard**

Create `tests/00-static/check_events_gateway.py` using the repo's `Result` helper and `return 0 if result.summary() else 1`. Pin:

```python
G1: gateway/Cargo.toml exists and contains 'name = "opensme-events-gateway"'
G2: gateway/src/main.rs contains '#[path = "../../portal/src/events.rs"] mod events;'
G3: main.rs has '.route("/api/events", get(handle_events))' and 'verify_session' + '401'
G4: session.rs has 'OPENSMEPORTALSESSION', 'HmacSha256', 'LOGIN_SESSION_COOKIE_SECRET'
G5: docker-compose.yml has 'events-gateway:' with 'profiles: ["events-gateway"]' and no publish route
G6: docs/EVENTS-GATEWAY.md exists and is linked from docs/README.md
```

Use exact text gates similar to `check_event_sync.py`. Keep `Result.summary()` as the final expression and the correct shell exit code.

- [x] **Step 2: Register and wire the guard**

In `tests/run.py`, after the `("SSE resume and dedupe", "tests/00-static/check_event_sync.py")` entry, add:

```python
("Events gateway", "tests/00-static/check_events_gateway.py"),
```

In `docs/VALIDATION.md`, add an inventory line for `check_events_gateway.py` next to the other event checks, naming the six pins.

In `tests/00-static/check_mutations.py`, add before the current last event mutation:

```python
{
    "name": "gateway-session-verification-removed",
    "file": "gateway/src/main.rs",
    "old": "    let session = match session::verify_session(&cfg, token.as_str()) {",
    "new": "    let session = match session::verify_session(&cfg, token.as_str()) { // MUTANT",
    "check": "tests/00-static/check_events_gateway.py",
    "why": "gateway — an unverified /api/events stream must fail Layer 0",
},
```

`// MUTANT` must make guard G3 fail; if it does not for a chosen anchor, change the mutation text to replace `verify_session` with `verify_sessionx` in a unique location and rerun.

- [x] **Step 3: Run RED**

```bash
python3 tests/00-static/check_events_gateway.py
```

Expected: all G1–G6 fail because the crate, docs and compose service do not exist yet.

- [x] **Step 4: Commit RED**

```bash
git add tests/00-static/check_events_gateway.py tests/run.py tests/00-static/check_mutations.py docs/VALIDATION.md
git commit -m "test(layer0): pin standalone events gateway (RED)"
```

### Task 2: Gateway crate

**Files:**
- Create: `gateway/Cargo.toml`
- Create: `gateway/src/session.rs`
- Create: `gateway/src/main.rs`
- Create: `gateway/Dockerfile`

- [x] **Step 1: Create the manifest**

```toml
[package]
name = "opensme-events-gateway"
version = "0.1.0"
edition = "2021"

[dependencies]
axum = "0.7"
tokio = { version = "1", features = ["full"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter"] }
hmac = "0.12"
sha2 = "0.10"
base64 = "0.22"
redis = { version = "0.27", default-features = false, features = ["tokio-comp"] }
tokio-stream = "0.1"
```

- [x] **Step 2: Create session verification with unit tests**

`gateway/src/session.rs` mirrors the portal cookie format exactly:

```rust
use axum::http::HeaderMap;
use base64::Engine;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

pub const SESSION_SECRET_MIN: usize = 32;
pub const PORTAL_SESSION_COOKIE: &str = "OPENSMEPORTALSESSION";

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Session {
    pub sub: String,
    pub name: String,
    pub email: String,
    pub exp: i64,
}

fn now() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}
fn b64(data: &[u8]) -> String { base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(data) }
fn unb64(s: &str) -> Option<Vec<u8>> { base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(s).ok() }

pub fn secret_from_env() -> Vec<u8> {
    std::env::var("LOGIN_SESSION_COOKIE_SECRET").unwrap_or_default().into_bytes()
}

pub fn verify_session(secret: &[u8], token: &str) -> Option<Session> {
    if secret.len() < SESSION_SECRET_MIN || secret.starts_with(b"CHANGEME_") { return None; }
    let (payload, sig) = token.split_once('.')?;
    let mut mac = HmacSha256::new_from_slice(secret).ok()?;
    mac.update(payload.as_bytes());
    mac.verify_slice(&unb64(sig)?).ok()?;
    let session: Session = serde_json::from_slice(&unb64(payload)?).ok()?;
    if session.exp < now() || session.sub.is_empty() { return None; }
    Some(session)
}

pub fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    let raw = headers.get(axum::http::header::COOKIE)?.to_str().ok()?;
    raw.split(';').find_map(|part| {
        let (key, value) = part.trim().split_once('=')?;
        (key == name).then(|| value.trim().trim_matches('"').to_string())
    })
}
```

Add `#[cfg(test)] mod tests` with a helper that signs a payload with a 32-byte secret and tests: valid token passes; tampered payload rejected; expired token rejected; weak secret rejected; `cookie_value` extracts `OPENSMEPORTALSESSION` from a Cookie header. Never log token contents.

- [x] **Step 3: Create the server**

`gateway/src/main.rs`:

```rust
#[path = "../../portal/src/events.rs"]
mod events;
mod session;

use axum::{
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Json, Response},
    routing::get,
    Router,
};
use serde::Deserialize;
use std::sync::Arc;

#[derive(Clone)]
struct AppState {
    cfg: events::StreamConfig,
    secret: Vec<u8>,
    tenant: String,
    bus: events::EventBus,
}

#[derive(Debug, Deserialize)]
struct EventsQuery {
    since: Option<String>,
}

async fn handle_events(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<EventsQuery>,
) -> Response {
    if state.bus.is_disabled() { return StatusCode::NO_CONTENT.into_response(); }
    let Some(token) = session::cookie_value(&headers, session::PORTAL_SESSION_COOKIE) else {
        return (StatusCode::UNAUTHORIZED, Json(serde_json::json!({"error":"session invalid or expired"}))).into_response();
    };
    let Some(s) = session::verify_session(&state.secret, &token) else {
        return (StatusCode::UNAUTHORIZED, Json(serde_json::json!({"error":"session invalid or expired"}))).into_response();
    };
    let last = headers.get("last-event-id").and_then(|v| v.to_str().ok()).map(str::to_string).or(query.since);
    let viewer = events::Viewer { sub: s.sub, email: s.email, name: s.name, tenant: state.tenant.clone() };
    state.bus.stream(viewer, last).await
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt().with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into())).init();
    let cfg = events::StreamConfig::from_env(|key| std::env::var(key).ok());
    let tenant = std::env::var("OPENSME_DOMAIN").unwrap_or_else(|_| "opensme.graphwiz.ai".into());
    let session_key = session::secret_from_env();
    let bus = events::EventBus::from_config(cfg.clone());
    let state = Arc::new(AppState { cfg: cfg.clone(), secret, tenant, bus });
    let app = Router::new().route("/api/events", get(handle_events)).with_state(state);
    let port = std::env::var("GATEWAY_PORT").ok().and_then(|v| v.parse().ok()).unwrap_or(8081);
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port)).await.expect("bind gateway");
    tracing::info!("events gateway listening on :{port}");
    axum::serve(listener, app).await.expect("serve gateway");
}
```

Add `#[cfg(test)] mod tests` using `axum::Router::into_service` with `tower::ServiceExt` only if a dev-dependency is added; otherwise test the session module and keep HTTP contract testing in the Layer-0 guard. Add `AppState.cfg` use via `let _ = &state.cfg;` if the compiler warns about an unused field, or remove the field from the struct entirely and keep only `secret`, `tenant`, `bus`. Prefer removing it.

- [x] **Step 4: Create the Dockerfile**

```dockerfile
FROM rust:1.98-slim-bookworm AS builder
WORKDIR /app
COPY portal/src/events.rs portal/src/events.rs
COPY gateway gateway
WORKDIR /app/gateway
RUN cargo build --release --bin opensme-events-gateway

FROM gcr.io/distroless/cc-debian12 AS runtime
COPY --from=builder /app/gateway/target/release/opensme-events-gateway /opensme-events-gateway
USER 1000:1000
EXPOSE 8081
CMD ["/opensme-events-gateway"]
```

The build context is the repo root so the shared `portal/src/events.rs` path resolves.

- [x] **Step 5: Format and check the guard**

```bash
cd gateway && cargo fmt --all && cargo fmt --all -- --check
python3 tests/00-static/check_events_gateway.py
```

Expected: fmt clean; G1–G4 pass, G5–G6 still fail before Task 3.

- [x] **Step 6: Commit the crate**

```bash
git add gateway
git commit -m "feat(events): add opt-in read-only events gateway crate"
```

### Task 3: Compose service and documentation

**Files:**
- Modify: `docker-compose.yml`
- Create: `docs/EVENTS-GATEWAY.md`
- Modify: `docs/README.md`

- [x] **Step 1: Add the compose service**

Add after the `portal:` service (before `redis:`):

```yaml
  events-gateway:
    <<: *service-defaults
    profiles: ["events-gateway"]
    build:
      context: .
      dockerfile: gateway/Dockerfile
    container_name: opensme-events-gateway
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    environment:
      EVENTS_ENABLED: ${EVENTS_ENABLED:-}
      REDIS_URL: redis://:${REDIS_PASSWORD:?REDIS_PASSWORD must be set in .env}@redis:6379
      EVENTS_STREAM: ${EVENTS_STREAM:-opensme:events}
      EVENTS_MAXLEN: ${EVENTS_MAXLEN:-10000}
      OPENSME_DOMAIN: ${OPENSME_DOMAIN:-opensme.graphwiz.ai}
      LOGIN_SESSION_COOKIE_SECRET: ${LOGIN_SESSION_COOKIE_SECRET:?}
      GATEWAY_PORT: 8081
      RUST_LOG: ${LOG_LEVEL:-info}
    depends_on:
      redis:
        condition: service_healthy
    networks:
      - opensme-net
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.events-gateway.rule=Host(`events.${OPENSME_DOMAIN:-opensme.graphwiz.ai}`) && PathPrefix(`/api/events`)"
      - "traefik.http.routers.events-gateway.entrypoints=websecure"
      - "traefik.http.routers.events-gateway.tls.certresolver=letsencrypt"
      - "traefik.http.services.events-gateway.loadbalancer.server.port=8081"
```

No publish endpoint is exposed; the only route is the read-only `/api/events`.

- [x] **Step 2: Write the runbook**

Create `docs/EVENTS-GATEWAY.md` with: purpose, exactly why it is opt-in, required env table, enable command (`COMPOSE_PROFILES=events-gateway docker compose up -d events-gateway`), verification with a valid `OPENSMEPORTALSESSION` cookie (`curl -N -H 'Cookie: OPENSMEPORTALSESSION=...' https://events.<domain>/api/events`), expected 401/204 responses, and rollback (`COMPOSE_PROFILES=events-gateway docker compose stop events-gateway`; portal `/api/events` remains the default). State explicitly that publishing stays in the portal.

- [x] **Step 3: Link the runbook**

Add `- [Events gateway](EVENTS-GATEWAY.md)` to the documentation list in `docs/README.md`, in the event/operations section.

- [x] **Step 4: Verify compose and the guard**

```bash
docker compose config --quiet
python3 tests/00-static/check_events_gateway.py
```

Expected: compose parses; G1–G6 all pass.

- [x] **Step 5: Commit compose + docs**

```bash
git add docker-compose.yml docs/EVENTS-GATEWAY.md docs/README.md
git commit -m "feat(events): add opt-in events-gateway compose service and runbook"
```

### Task 4: Closeout and full verification

**Files:**
- Modify: `docs/superpowers/plans/2026-11-27-events-gateway.md`
- Modify: `docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`

- [x] **Step 1: Close the plan**

Replace every `- [ ]` step in this plan with `- [x]`, except the line-3 syntax example.

- [x] **Step 2: Annotate the follow-up**

In `docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`, add to follow-up item 3 that the standalone gateway landed 2026-11-27: design `033209d`, plan `e074cbe`, commits from this task, and that the gateway is opt-in and read-only.

- [x] **Step 3: Full verification**

```bash
python3 tests/run.py --static
python3 tests/00-static/check_mutations.py
cd gateway && cargo fmt --all -- --check
cd ../portal/web && bun run test && bun run typecheck
```

Expected: static gate green, mutation suite green with the new gateway mutation targeted, fmt clean, vitest and typecheck unchanged. Do **not** run `cargo build`/`cargo test` locally (AGENTS.md); `vhrz2392` was unreachable earlier, so CI/remote is the compile gate.

- [x] **Step 4: Commit and push**

```bash
git add docs/superpowers/plans/2026-11-27-events-gateway.md docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md
git commit -m "docs(events): close standalone gateway plan and follow-up"
git push
```
