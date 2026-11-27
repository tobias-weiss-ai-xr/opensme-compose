# Standalone Events Gateway — Design

**Date:** 2026-11-27
**Status:** Approved

## Goal

Provide an opt-in, read-only SSE gateway that can serve the portal's event
stream independently of the Rust portal process, without duplicating the
event semantics that already exist in `portal/src/events.rs`.

## Context

Today the only SSE endpoint is `GET /api/events` on the protected router of
the portal (`portal/src/main.rs`). The broadcast consumer already runs in a
separate Next.js process (`portal/web/instrumentation.ts`). If suite-wide load
ever warrants isolating stream readers from portal writes, the stream should
be movable without rewriting framing, visibility, or resume behaviour.

## Architecture

- New opt-in crate `gateway/` (package `opensme-events-gateway`) with its own
  `Cargo.toml`, `src/main.rs`, and multi-stage `Dockerfile` (rust builder →
  debian-slim runtime, non-root user).
- `gateway/src/main.rs` includes `portal/src/events.rs` as a path module:
  `#[path = "../../portal/src/events.rs"] mod events;`. This makes `EventBus`,
  `Viewer`, `Visibility`, `sanitize_cursor`, `XREVRANGE` resume, SSE framing,
  visibility filtering, heartbeats, and the per-viewer stream cap the same
  code in both processes. No changes to `portal/src/events.rs` are part of
  this slice.
- The gateway does **not** publish events. Publishing stays in the portal.
  The gateway only reads the Redis stream and serves SSE.
- Session verification is implemented locally in
  `gateway/src/session.rs` with the same format and crypto as
  `portal/src/session.rs`: `OPENSMEPORTALSESSION` cookie, URL-safe no-pad
  base64 of JSON `Session { sub, name, email, exp }`, dot-separated URL-safe
  no-pad base64 HMAC-SHA256 over the payload using
  `LOGIN_SESSION_COOKIE_SECRET`, `exp` in Unix seconds, fail-closed on
  missing/weak secret, malformed value, bad signature, empty `sub`, or
  expiry. Parsing this payload is deliberately minimal; reusing the whole
  portal `session.rs` would pull the portal's `AppConfig` and OIDC client into
  the gateway.

## Endpoints and data flow

```text
browser --Cookie: OPENSMEPORTALSESSION--> gateway:8081 /api/events?since=...
          gateway verifies cookie locally -> Viewer { sub, name, email, tenant }
          gateway -> Redis XREAD/XREVRANGE via events::EventBus::stream
          gateway -> browser SSE frames (same event:/id:/data: format)
```

- Route: `GET /api/events` only. No publish endpoint, no wildcard proxy.
- Query `since` is parsed into `Option<String>` and passed to
  `EventBus::stream` exactly as the portal handler does (header value takes
  precedence, then query). Cursor sanitisation and replay stay in the shared
  module.
- Anonymous / tampered / expired cookie → `401` with
  `{"error":"session invalid or expired"}`. No `Viewer`, no Redis call.
- Disabled `EVENTS_ENABLED` → `204`, matching the portal contract.
- Same `event: ready` synthetic frame and keep-alive comments as the portal,
  because both come from the shared module.

## Configuration

| Variable | Required | Meaning |
|----------|----------|---------|
| `EVENTS_ENABLED` | no (`1`/`true`) | Enables stream reads; otherwise 204 |
| `REDIS_URL` | yes when enabled | `redis://:password@redis:6379` |
| `EVENTS_STREAM` | no | Defaults to `opensme:events` |
| `OPENSME_DOMAIN` | yes | Tenant used when building `Viewer` |
| `LOGIN_SESSION_COOKIE_SECRET` | yes | HMAC key; <32 bytes or `CHANGEME_` prefix = fail-closed |
| `GATEWAY_PORT` | no | Listen port, default `8081` |

The gateway shares the Redis URL and stream name with the portal; it does not
need IdP URLs, client secrets, or database credentials.

## Deployment

- Compose service `events-gateway` lives in `docker-compose.yml` under
  `profiles: ["events-gateway"]`, so default deployments are unchanged.
- It uses `build: ./gateway`, `restart: unless-stopped`, `init: true` if the
  runtime image lacks its own init, resource limits consistent with other
  small services, and joins `opensme-net`.
- Traefik labels for `Host(events.<OPENSME_DOMAIN>)` and `/api/events` are
  present **only** on this service; the portal's own `/api/events` remains the
  default route when the profile is off.
- `docs/EVENTS-GATEWAY.md` documents how to enable the profile, required env,
  verification (`curl -i` with a valid session cookie), and rollback.

## Testing

- New Layer-0 guard `tests/00-static/check_events_gateway.py` pins:
  1. crate manifest exists with package name `opensme-events-gateway`;
  2. `gateway/src/main.rs` includes `portal/src/events.rs` via `#[path = ...]`;
  3. the route is `GET /api/events` and is behind local cookie verification
     (no unauthenticated stream);
  4. `OPENSMEPORTALSESSION` + HMAC-SHA256 verification with
     `LOGIN_SESSION_COOKIE_SECRET`, plus fail-closed 401;
  5. compose service is under profile `events-gateway`, exposes only the
     gateway port/route, and has no publish endpoint;
  6. `docs/EVENTS-GATEWAY.md` exists and is linked from `docs/README.md`.
- Registered in `tests/run.py`, inventoried in `docs/VALIDATION.md`, and given
  a mutation with teeth in `tests/00-static/check_mutations.py` (remove the
  cookie-verification call from the gateway handler → guard must fail).
- Rust unit tests in `gateway/src/session.rs` cover valid, tampered, expired,
  and missing-secret tokens.
- Verification: `python3 tests/run.py --static`, mutation suite, compose parse,
  `cargo fmt --all -- --check` in `gateway/`. Per `AGENTS.md`, Rust builds/tests
  run only on remote host `vhrz2392`; locally we use fmt + static gate + CI.

## Non-goals

- Moving event publishing out of the portal.
- WebSocket transport, external push notifications, or removing the existing
  polling fallback.
- Adding a second Redis stream or changing event payload schemas.
- Enabling the gateway by default.
