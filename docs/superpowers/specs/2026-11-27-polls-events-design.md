# Design: polls realtime events (realtime-events follow-up 3)

## Context

The portal event stream already carries `task.*`, `intercom.*`, cloud and
session events, and the browser reconciles those panels live. Polls are the
remaining first-party surface: `PollsPanel.tsx` fetches once on mount and
again only after its own mutations, so other users' new polls and votes stay
invisible until a manual reload. The original realtime design explicitly
listed "add polls events" as optional follow-up 3.

## Goals / Non-Goals

**Goals:** publish poll creation and vote events, and make the polls panel
consume them live, including reconciliation after `portal.ready`.

**Non-Goals:** no LimeSurvey/surveys changes, no poll deletion events, no
schema or route changes, no per-recipient fanout (polls are a shared team
surface).

## Architecture

Backend (`portal/src/main.rs`, using the existing `EventBus`):

| Handler | Event kind | Payload |
|---|---|---|
| `handle_polls_create` | `poll.created` | `{"poll": <PollPublic>}` |
| `handle_polls_upload` | `poll.created` | `{"poll": <PollPublic>}` |
| `handle_polls_vote` | `poll.voted` | `{"poll": <PollPublic>}` |

All three publish only after a successful store operation, with
`actor_sub = session.sub`, `tenant = config.opensme_domain` and
`Visibility::Team`. The payload uses `poll.public(None)` (the viewer-neutral
`PollPublic`): it contains tally and metadata but never `voted_by` or a
per-viewer `voted` flag. Publishing is fail-soft (`let _ = … .await`), so a
Redis outage never breaks a vote or creation.

Frontend (`portal/web`):

- `useEventStream.ts` `LISTENED_KINDS` gains `poll.created` and `poll.voted`.
- `PollsPanel.tsx` subscribes with `subscribeEvents`: `poll.created` prepends
  an unseen poll; `poll.voted` replaces the matching poll's tally while
  preserving the local `voted` flag; `portal.ready` triggers `refresh()` —
  the same reconciliation pattern as `IntercomPanel`/`TasksPanel`.
- No new fetch route or state store; the existing `/api/polls` GET remains
  authoritative after a ready event.

## Error handling and privacy

- A malformed event payload is ignored (the listener validates the payload
  shape before merging); stream failures stay in the existing SSE/polling
  fallback.
- Team visibility is appropriate: poll questions, options and tallies are
  already visible to every authenticated portal user through `/api/polls`.
- `voted_by` subjects are internal and must never cross the stream; the
  Layer-0 guard pins that.

## Testing

- New `tests/00-static/check_polls_events.py` pins the three publish sites
  (kind, Team visibility, `.event_bus`, viewer-neutral `poll.public(None)`),
  the frontend listener kinds, the panel subscription with ready
  reconciliation, and absence of `voted_by` in event payloads.
- Register in `tests/run.py` and `docs/VALIDATION.md`; add a mutation with
  teeth in `check_mutations.py` (removing one poll publish must fail the
  guard).
- Rust unit tests in `main.rs` for the event payload shape/visibility;
  vitest tests in `PollsPanel.test.tsx` for poll.created/poll.voted/ready.
- Acceptance: static gate green (including mutation coverage), vitest and
  typecheck green, `cargo fmt --check` clean. `cargo test` only on
  `vhrz2392` per AGENTS.md; CI is the local compile gate.

## Risks / Rollback

- Duplicate polls on reconnect: the ready reconciliation replaces the list;
  the created-event merge deduplicates by id.
- Stale tally after simultaneous votes: last event wins until the next
  refresh; acceptable for a shared team board.
- Rollback is a revert of the producer/consumer commits; the stream simply
  stops carrying the new kinds and the panel falls back to its old fetch
  cadence.
