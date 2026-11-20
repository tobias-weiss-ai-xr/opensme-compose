## Context

The portal already has the pieces this change composes: a signed-session
firewall (`require_session`, session carries sub/name/email as
`Extension<Session>`), a JSON-persisted capped store pattern (intercom),
a typed fail-soft frontend bridge (`lib/bridge.ts` with `share.open` /
`cloud.pick`), and one-tab-per-panel shell layout (`PortalShell`). Tasks are
portal-native: no new compose service, no new tile, no IdM coupling. See
proposal.md for motivation and specs/ for the behavior contract.

## Goals / Non-Goals

**Goals:** one Aufgaben tab with create/assign/due/priority/done/delete;
`task.create` bridge slot fed by feed, note and cloud-row affordances;
bounded JSON persistence; permission rule (assignee-or-author) enforced
server-side.

**Non-Goals:** landing widget for open tasks, share-link minting for cloud→task,
task comments, cross-search, IdM user-list integration, notifications beyond
polling. All are follow-up deltas on the same slot/store.

## Decisions

- **New `portal/src/tasks.rs` module + routes in `main.rs` protected router.**
  Mirrors intercom/polls structure; keeps `main.rs` thin. Alternative: extend
  intercom store — rejected, tasks are not ephemeral FIFO messages.
- **Store: JSON file via `TASKS_DATA_PATH`, atomic tmp+rename, reload on boot;
  in-memory when unset; cap 500 evicting oldest `done` first.** Same pattern
  as intercom (proven, testable) minus the FIFO cap — tasks are work items.
  Eviction of done-only keeps open work safe.
- **Author from session, never client-supplied.** `POST /api/tasks` reads
  `Extension<session::Session>`; assignee is a plain string chosen from the
  `/api/tasks/users` dropdown (seeded from intercom authors + task
  authors/assignees) or free text. Alternative: Zitadel user list — rejected,
  breaks IdM-swap cleanliness for little gain at SME scale.
- **Permission check in the handler, not middleware.** PATCH/DELETE compare
  `task.assignee`/`task.author` against session email or name
  (case-insensitive); mismatch → 403. Anyone may create/assign.
- **Bridge slot `task.create` with payload `{ title, url? }`.** Shell registers
  it exactly like `share.open`: switch tab + prefill create form. Consumers
  (FeedColumn, IntercomPanel, CloudFilePicker) call `useSlot("task.create")`
  and hide the button when absent (fail-soft). Cloud row passes file name +
  path in description, no share-link minting in v1.
- **Frontend: `TasksPanel.tsx` in a new "Aufgaben" tab**, polling
  `GET /api/tasks?since=` like the intercom panel; filter
  alle/meine/offen/erledigt; sort due→priority; overdue highlight; inline 403.
- **German-only UI**, consistent with the rest of the portal.

## Risks / Trade-offs

- [Store file grows on long installs] → Mitigation: 500 cap evicting oldest done.
- [Assignee string drift: "Anna" vs "anna@firma.de" both used] → Mitigation:
  dropdown seeds canonical values, comparison is case-insensitive; a later
  delta can normalize to email.
- [Polling misses updates between visits] → acceptable for v1 (same as
  intercom); push/websocket is out of scope.
- [403 inline handling must not lose form state] → panel keeps draft in local
  state, shows error, does not clear.

## Migration Plan

No data migration: new store starts empty; `TASKS_DATA_PATH` unset = in-memory
(dev default). Deploy = rebuild portal + portal-web containers, recreate; the
protected router means old anonymous probes still get 401. Rollback = previous
images; no schema migration exists.

## Open Questions

None — all scope decisions were made with the user during proposal (assignee
model B, fields B, permissions B, cap B, consumers C, single tab A).
