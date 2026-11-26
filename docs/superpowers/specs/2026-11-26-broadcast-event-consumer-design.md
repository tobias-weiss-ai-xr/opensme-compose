# Design: event-driven Matrix/mail broadcast consumer (realtime-events slice 2)

## Context

`portal/web/src/app/api/broadcast/route.ts` currently stores an operator
broadcast note through the Rust `/api/intercom` route and then calls
`fanoutBroadcast` directly, returning per-channel results to the browser.
The realtime-events design lists "Matrix/mail broadcast becomes event-driven
consumers" as follow-up slice 2. The Redis event stream and the portal's SSE
proxy are already deployed; the portal-web container can reach the same Redis
instance.

## Goals / Non-Goals

**Goals:**
- Turn the HTTP POST into a queue operation: publish `broadcast.requested`
  to the existing stream and return immediately.
- Move fanout execution into a portal-web instrumentation worker that
  consumes the stream with a consumer group and acknowledges only success.
- Keep the existing Matrix/mail fanout implementation and its best-effort,
  idempotent behaviour unchanged.

**Non-Goals:**
- No Rust-side consumer, no callback from the backend into portal-web.
- No changes to `fanoutBroadcast`, `broadcast-fanout.sh`, or the Matrix/mail
  message formats.
- No direct SOGo/OpenCloud/IdP producers (follow-up 1 remains separate).

## Architecture

| Component | Responsibility |
|---|---|
| `POST /api/events/publish` (Rust, protected) | Accept `{"kind":"broadcast.requested","text":…}`; reject other kinds with 400; publish with actor from verified session, tenant `config.opensme_domain`, `Visibility::Team`; return 202 |
| `portal/web/src/app/api/broadcast/route.ts` | Store note via `/api/intercom`, forward browser cookie to `/api/events/publish`, return `{message, queued:true}` |
| `portal/web/instrumentation.ts` (Node.js only) | On `EVENTS_ENABLED`, connect to `REDIS_URL`, consume `EVENTS_STREAM` with group `portal-web-broadcast`, call `fanoutBroadcast(text)`, then `XACK` |
| `IntercomPanel` | Show a queued confirmation instead of per-channel result badges |

The worker uses one consumer name per process (`hostname`-derived). On
startup it runs `XAUTOCLAIM` for entries pending longer than five minutes so
a crashed worker does not strand broadcasts. A failed fanout is logged and
left unacknowledged; it is retried by the next reclaim. Duplicate delivery is
safe because the Matrix leg uses an idempotency transaction id and the mail
leg uses the existing script's dedupe behaviour.

No service token is introduced: the publish endpoint is inside
`session::require_session`, so the web route forwards the browser's cookie.
Only authenticated users can enqueue broadcasts, matching today's gate.

## Data flow and error handling

1. Browser POST → web route → backend `/api/intercom` (note stored; 401/403
   propagated).
2. Web route forwards cookie + JSON to backend `/api/events/publish`. A
   rejected publish leaves the note stored and surfaces the status as before.
3. Worker reads the stream, extracts `payload.text`, calls
   `fanoutBroadcast(text)`, then ACKs. Errors are logged; no ACK.
4. UI renders `Broadcast vorgemerkt …` (queued), with the stored note still
   prepended to the list.

Redis and events-disabled cases mirror today's best-effort posture: the note
is never lost, and fanout failures never block the UI.

## Testing

- New `tests/00-static/check_broadcast_consumer.py`, registered in
  `tests/run.py`: pins the protected publish route and kind allowlist, Team
  visibility, web route queueing (no direct `fanoutBroadcast` import there),
  instrumentation consumer group + XACK/XAUTOCLAIM, and queued UI status.
- Mutation entry with teeth (remove the worker's fanout call) and inventory
  line in `docs/VALIDATION.md`.
- Vitest: route publishes `{kind,text}` and returns `queued`; worker handler
  ACKs on success and does not ACK on failure; existing `broadcast.test.ts`
  and `IntercomPanel.test.tsx` adjusted to the new UI contract.
- Acceptance: `python3 tests/run.py --static` and `bun run test` /
  `bun run typecheck` in `portal/web` green; `cargo fmt --all -- --check`
  clean; remote `cargo test` on `vhrz2392` when reachable (CI compiles
  otherwise; AGENTS.md forbids local portal builds).

## Risks / Rollback

- New `ioredis` dependency in portal-web — pinned in `package.json` and
  covered by the existing lockfile.
- Consumer group state lives in Redis; rollback to the direct fanout route
  leaves at most unacknowledged entries, which expire with the stream's
  `maxlen`.
- Worker duplicated across multiple web replicas is safe via consumer groups
  and the fanout idempotency key.
