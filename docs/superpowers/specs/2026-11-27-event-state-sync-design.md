# Richer Client State Sync (SSE Resume + Dedupe) Design

**Date:** 2026-11-27
**Status:** Approved for implementation

## Goal

Make the portal's existing SSE event stream survive reconnects and page reloads
without losing or replaying duplicate side effects in the UI. This is the
"richer client state sync" follow-up from
`docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`.

## Context

The stream already carries a Redis stream ID in `Event.id` and emits it as the
SSE `id:` field. `handle_events` forwards the browser's `Last-Event-ID` header,
and `events.rs` validates it with `sanitize_cursor`. Three gaps remain:

1. `ensure_cursor_live()` peeks the newest entry with `XRANGE + - COUNT 1`.
   Redis treats `+` as the maximum and `-` as the minimum, so this returns the
   *oldest* entry; a valid resume cursor is reset to `$` and events are missed.
2. Full page reloads create a fresh `EventSource` with no `Last-Event-ID`.
3. `useEventStream` dispatches every frame without checking the SSE ID, so a
   replay after reconnect can apply the same delta twice.

Panels already reconcile authoritative state on `portal.ready` and deduplicate
domain objects by their own IDs, so this change is limited to transport-level
resume and dispatch-level dedupe. No producer, panel, or upstream service
changes are required.

## Architecture

### Backend (`portal/src/events.rs`, `portal/src/main.rs`)

- Replace the cursor-live peek command with `XREVRANGE <stream> + - COUNT 1`
  (same reply shape, truly newest entry first). Keep `sanitize_cursor`,
  `stream_id_key`, the `newest < cursor` reset, and the `if !ready &&
  cursor_checked` ready-frame gate unchanged.
- Add `EventsQuery { since: Option<String> }` to `handle_events`. Effective
  cursor is `HeaderMap("last-event-id")` first, then `?since=`. This keeps
  native `EventSource` reconnect behavior and adds an explicit resume for full
  reloads. `sanitize_cursor` remains the single validation point.
- Make `EventBus::replay` honor `since` for the in-memory backend by comparing
  numeric `m-N` IDs; the Redis backend keeps returning an empty replay because
  live readers handle that case. This keeps tests and disabled-Redis deployments
  deterministic.

### Proxy (`portal/web/src/app/api/events/route.ts`)

- Append `req.nextUrl.search` to the backend fetch URL so `?since=` reaches
  `handle_events`.
- Continue forwarding `cookie`, `user-agent`, `last-event-id`, and the
  `text/event-stream` accept header; keep streaming `upstream.body` unchanged.

### Client (`portal/web/src/lib/useEventStream.ts`)

- On mount, read the last seen SSE ID from `sessionStorage`
  (`opensme:events:last-id`) and open `new Ctor(\`/api/events?since=${id}\`)`
  when present, otherwise `/api/events`.
- In each kind listener, compute `frameId = MessageEvent.lastEventId ||
  event.id`. Skip `dispatchEvent` when `frameId` is already in a bounded
  in-memory seen set (max 512 IDs). Otherwise record it, persist the bounded
  set to `sessionStorage`, and dispatch.
- The synthetic `sse-ready` dispatch remains unconditional so the existing
  panel reconciliation contract is preserved.
- Keep the polling fallback and `data-events-connected` behavior unchanged.

## Error handling and limits

- Invalid/stale `since` values are sanitized to `$` server-side; no new error
  surface is exposed to the client.
- `sessionStorage` may be unavailable (private mode / disabled storage); reads
  and writes are wrapped so the hook still works with an in-memory set only.
- The seen set is bounded at 512 to prevent unbounded growth on long-lived
  tabs; domain-level panel dedupe remains the authoritative guard.
- Events disabled (`204`) still terminate the `EventSource` and leave polling
  active, unchanged.

## Testing

- **Layer-0:** update `tests/00-static/check_events.py` for the relaxed hook URL
  regex and `XREVRANGE`; add `tests/00-static/check_event_sync.py` pinning the
  query extractor, proxy search forwarding, newest-entry peek, bounded seen
  set, `sessionStorage` key, and unconditional ready dispatch. Register it in
  `tests/run.py`, add a `docs/VALIDATION.md` inventory line, and add mutation
  `sse-resume-cursor-broken` (revert `XREVRANGE` to `XRANGE`) with teeth.
- **Rust:** unit test that `EventBus::replay` with `since` filters in-memory
  events by ID.
- **Vitest:** update `useEventStream.test.ts` URL expectations and add tests for
  (a) resume query from stored ID, (b) duplicate frame suppression, (c) ready
  dispatch still emitted; update `route.test.ts` to assert `?since=` forwarding.
- **Verification:** `python3 tests/run.py --static`, `check_mutations.py`,
  `bun run test`, `bun run typecheck`, `cargo fmt --all -- --check` locally.
  No local `cargo build` (AGENTS.md: remote `vhrz2392` only; currently
  unreachable).

## Non-goals

- No new event kinds, no snapshot versioning, no change to panel merge logic.
- No upstream SOGo/OpenCloud/IdP changes.
- No standalone gateway extraction (tracked as a separate follow-up).

## Follow-up annotation

On completion, annotate follow-up item 3 in
`docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md` as done for
client state sync, with design/plan and commit IDs.
