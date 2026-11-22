# Design: Portal real-time events (slice 1 of suite-wide intercom)

Date: 2026-11-21
Status: design approved by user; awaiting written-spec review

## Context and goal

The portal's intercom (notes), team tasks, polls and feed currently update via a
7-second delta poll (`GET /api/intercom?since=…`) plus manual refresh. The user
asked to "further improve intercom between the apps", chose **real-time between
apps**, scoped the ambition to the **whole suite including Matrix/mail**, and
approved decomposing that into slices. This spec covers **slice 1: the shared
event backbone plus portal real-time**. Later slices (OpenCloud/SOGo producers,
Matrix/mail event consumers) get their own specs and will plug into the same
backbone.

The existing stack already has a single always-on Redis (`opensme-redis`,
persistent `appendonly yes`). OpenCloud has its own internal NATS; that is not
reused here. There is no active opsx intercom change — both prior intercom
changes are archived with all tasks done — so this is a fresh addition, not a
modification of an in-flight change.

## Decisions (confirmed with the user)

| Decision | Choice |
|---|---|
| Scope of slice 1 | Event backbone + portal real-time (foundation + visible value) |
| Browser transport | SSE (`text/event-stream`) |
| Backbone semantics | Redis Streams with per-connection XREAD cursors (broadcast, replayable) |
| Event scoping | Single stream, server-side visibility filter after session auth |
| Degradation | Fail-soft: SSE is an enhancement, 7s polling stays as fallback |
| Implementation approach | In-process Rust event module in the portal (no new service) |

## Architecture

```
browser (EventSource) ── /api/events ──> Next.js streaming proxy ──> Rust portal
                                                                       │
                                          require_session + filter      │
                                                                       ▼
                                                              Redis Stream opensme:events
                                                                       ▲
                                          publish from intercom/task handlers
```

No new externally reachable service or port is introduced. Redis stays on the
internal compose network. The portal holds the only `EventBus`; future services
will publish/consume the same stream in later slices.

## Components

### `portal/src/events.rs` (new module)

- `Event` payload: `{ kind: String, actor_sub: String, tenant: String,
  visibility: Visibility, payload: serde_json::Value, created_at: i64 }`.
  `Visibility::Team` (all authenticated users) or
  `Visibility::Recipients(Vec<String>)` (author + assignee). The server-side
  viewer matches recipients against OIDC `sub`, email and display name — the
  stores stamp people by email/name, so matching only `sub` would silently drop
  every task event for non-actors while polling is paused. Events are also
  tenant-scoped; the stream is shared with future producers. Redis assigns the
  stream ID; that ID becomes the SSE `id:`.
- `EventSink` trait: `publish(&self, event) -> Result<()>`. Two implementations:
  - `RedisEventBus` — `XADD key MAXLEN ~ <maxlen> * <fields>` via the `redis`
    async crate (new dependency). Connection is created lazily; the bus is
    `Option<EventSink>` in `AppConfig` so an empty/disabled config is a no-op.
  - `InMemoryEventSink` (test-only) — records events, no Redis required.
- `subscribe(...)`: an async stream that reads with `XREAD COUNT 10 BLOCK 1000
  STREAMS <stream> <cursor>`. **No consumer group**: a shared group is a work
  queue (one consumer per entry) and would silently drop events for all but one
  viewer, and could deliver a private event to a viewer who filters it out while
  the real recipient never sees it. Each connection owns its cursor: on connect
  with `Last-Event-ID` the cursor is that ID (XREAD returns entries strictly
  after it), otherwise `$` (new entries only). This broadcasts every entry to
  every connection, needs no consumer/ack bookkeeping, and gives at-least-once
  replay after reconnect via the browser-supplied cursor. Each yielded event is
  serialized as `event: <kind>\nid: <redis-id>\ndata: <json>\n\n`.
- Heartbeat `: keep-alive\n\n` after each XREAD cycle (which blocks at most
  1 s). There is deliberately **no** sleep between cycles: a 15 s sleep after
  a 1 s read would hold a just-published event for up to 15 s, defeating
  real-time delivery. Response headers:
  `Content-Type: text/event-stream`, `Cache-Control: no-cache, no-transform`,
  `X-Accel-Buffering: no`, `Connection: keep-alive`.
- Cap concurrent SSE connections per session (3) — a 4th gets `429` and keeps
  polling. The first successful XREAD emits an explicit `event: ready` frame;
  the client pauses polling only then, so an event posted in the header→XREAD
  window is not missed by both paths. The pending queue is bounded (16 frames);
  backpressure blocks that reader task rather than growing memory, and the slot
  is released when the client disconnects. A separate idle timeout is not used:
  the 1 s keep-alive already bounds liveness detection, and a healthy idle
  stream is the desired state.

### Publishers (handlers in `portal/src/main.rs`)

Emit through `config.event_bus` (fail-soft: no bus → no event):

- `handle_intercom_post` → `intercom.created` `{id, author, kind, attachment?}`,
  `Visibility::Team`.
- `handle_intercom_delete` → `intercom.deleted` `{id}`, `Visibility::Team`.
- tasks create/patch/delete handlers → `task.created` / `task.updated` /
  `task.deleted` with `{id, assignee, ...}`, `Visibility::Recipients(author +
  assignee)`.
- Polls and feed are explicitly **out of scope** for slice 1: feed is external
  RSS (no local event source); polls already reuse the intercom store and can be
  added in a later slice.

### SSE endpoint

`GET /api/events`, added to the protected router alongside intercom/tasks routes
(`main.rs` around 1095–1154) so it inherits `require_session`. Handler:
extract `Extension<Session>`, if no bus → return `204 No Content` (EventSource
treats 204 as terminal and stops reconnecting; the client keeps polling), else
return a streaming axum body from `subscribe(session, last_id)`.
Read `Last-Event-ID` from the request headers (`axum::http::header::HeaderMap`).

### Frontend streaming proxy

`portal/web/src/app/api/events/route.ts` (new, dedicated): `export const dynamic
= "force-dynamic"`, nodejs runtime. Forwards the incoming `cookie` and
`Accept: text/event-stream` (plus `user-agent`) to `${backendUrl()}/api/events`,
then returns the upstream body as a `ReadableStream` via `new Response(upstream.body,
{ headers: { "content-type": "text/event-stream", "cache-control": "no-cache,
no-transform" } })`. Abort the upstream on client disconnect. The existing
catch-all `/api/[[...path]]` proxy is left unchanged (it buffers whole bodies and
must not be used for SSE). Same-origin keeps CSP `connect-src 'self'` intact.

### Frontend hook

`portal/web/src/lib/useEventStream.ts` (new):
- `EventSource('/api/events')`, browsers reconnect with `Last-Event-ID`
  automatically.
- `onmessage`/typed listeners dispatch events into existing state via the
  current `mergeIncoming` / task-setters (reuse, do not duplicate reducers).
- Exposes `{ connected: boolean }`. On `onerror` (or `204` on open), set
  `connected=false`; the existing 7 s poller resumes. While `connected=true`,
  suppress the poller to avoid duplicate loads.
- Mounted once near the existing providers in `PortalShell`.

### Traefik

The `compress@docker` middleware (`docker-compose.yml:141`,
`traefik.http.middlewares.compress.compress=true`) must explicitly exclude
`text/event-stream` from compression so it does not buffer the stream. Add
`traefik.http.middlewares.compress.compress.excludedContentTypes=text/event-stream`
to that router's labels (verify the exact v3 label key against the running
Traefik version during implementation).

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `EVENTS_ENABLED` | empty (off) | Enables the bus; demo/CI set to `1` |
| `REDIS_URL` | `redis://:.password@redis:6379` (compose-internal) | Redis connection |
| `EVENTS_STREAM` | `opensme:events` | Stream key (prefixable per deployment) |
| `EVENTS_MAXLEN` | `10000` | Approx. stream cap (`MAXLEN ~`) |

Wire into `docker-compose.yml` portal env, `.env.example`, and
`contracts/interop.yml`. No new port published. Live deployment is not changed
by this slice.

## Security

- `GET /api/events` is behind `require_session`; the Next proxy forwards the
  HttpOnly session cookie; the client cannot select an arbitrary channel.
- Visibility filtering runs server-side **before** serialization and includes
  a tenant check. Recipient lists hold store identifiers (OIDC `sub`, email,
  display name); a user never receives another's private note or task
  payload/metadata. Entries with missing/invalid visibility metadata fail
  closed.
- `Last-Event-ID` is a read cursor only, never an authorization input; malformed
  IDs reset to `$`.
- Redis password required; no secrets in event payloads; no new public port.
- Per-session connection cap (3) bounds resource usage; keep-alive frames make a
  separate idle timeout redundant.

## Error handling and degradation

- A malformed Redis URL disables the bus at startup (`204` from SSE). A reachable
  but unavailable Redis cannot be detected at construction (URL parsing does not
  connect), so repeated read failures terminate the SSE body after 3 attempts.
  `EventSource.onerror` then resumes 7 s polling and the browser retries with
  backoff; writes never fail because publishing is fail-soft.
- Stream drops → `EventSource` reconnects with `Last-Event-ID`; XREAD from that
  cursor replays entries after it; polling resumes immediately on error and is
  suppressed only after the `ready` frame confirms the reader is live.
- Next proxy aborts upstream on client disconnect; backend `401`/`502` ends the
  stream so the client retries rather than hanging.
- Backpressured/slow consumers are closed and rely on replay.

## Testing

- Rust unit: `EventSink` in-memory impl; event serialization; visibility filter;
  HTTP contract via `router::oneshot` — `401` without session, event emitted
  after `POST /api/intercom`, private task not leaked to another session.
- Frontend vitest: `useEventStream` — dispatch into notes/tasks state,
  `Last-Event-ID` reconnect, fallback to polling on error/`204`, single mount.
- Static Layer 0: `tests/00-static/check_events.py` pins the endpoint behind
  `require_session`, no Redis host exposure in client code, and every publish
  site passes an explicit visibility scope.
- E2E: `tests/05-e2e/realtime.py` — two sessions, one posts a note/task, the
  other sees it within ~2 s; a Team liveness marker proves the stream is alive,
  the actor's own `task.created` proves the task event was actually published,
  then the unrelated viewer's absence of it is asserted. Polling fallback when
  events disabled. Wire into the CI SSO job via `scripts/ci-e2e-step.sh`.
- Docs: update `docs/ARC42.md` §7, `docs/VALIDATION.md`,
  `docs/E2E-JOURNEYS.md` with the new event path and journey.

## Non-goals for slice 1

OpenCloud/SOGo producers; Matrix/mail event consumers; WebSocket; removing the
7 s polling fallback; external push notifications; polls/feed events.

## Follow-up slices (each its own spec → plan → implementation)

1. OpenCloud/SOGo producers publish file/share/SSO events to the stream.
2. Matrix/mail broadcast becomes event-driven consumers (replacing today's
   direct `fanoutBroadcast` calls).
3. Optional: add polls events, richer client state sync, extract the bus into a
   standalone gateway if suite-wide load demands it.
