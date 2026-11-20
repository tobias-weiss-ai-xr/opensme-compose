## Why

An SME team of 5–50 people lives in two modes: *discussing* (Notizen) and *deciding* (Umfragen) — but there is no accountable "who does what by when". Work items today are buried in note text, and approvals sit invisible in the Camunda tile. The portal needs a first-class task list so that "follow up on this article / turn this note into work / review this file" becomes one click instead of a copied text.

## What Changes

- New portal-native **Aufgaben tab** with a task list (create, assign, due date, priority, done-toggle, delete) — no new service, no new tile.
- New Rust module `portal/src/tasks.rs` + endpoints `GET/POST /api/tasks`, `PATCH/DELETE /api/tasks/:id`, `GET /api/tasks/users` — all behind the existing `require_session` firewall, JSON-persisted via `TASKS_DATA_PATH` (atomic tmp+rename), capped at 500 tasks evicting oldest *done* first.
- New bridge slot **`task.create`** in `portal/web/src/lib/bridge.ts`; `PortalShell` registers it (switch to Aufgaben tab + prefill) and adds the tab.
- "Als Aufgabe anlegen" affordances in **FeedColumn** (per article), **IntercomPanel** (per note) and **CloudFilePicker** (per file row).
- Assignee picker = dropdown of known team members (derived from note/task authors + assignees via `GET /api/tasks/users`) + free-text fallback. No IdM coupling (Zitadel↔Casdoor swap stays clean).
- Permissions: any signed-in member may create and (re)assign; **only assignee or author may mark done or delete**.

## Capabilities

### New Capabilities
- `portal-tasks`: the task model, API, permissions, persistence cap and bridge slot for portal-native team tasks.

### Modified Capabilities
- `portal-intercom`: the bridge gains the `task.create` slot and three consumer affordances (feed, note, cloud row) — delta spec extends the existing registry/share requirements.

## Impact

- `portal/src/tasks.rs` (new), `portal/src/main.rs` (routes + store wiring), `portal/src/intercom.rs` (read-only author listing helper if needed).
- `portal/web/src/lib/bridge.ts` (new slot), `components/PortalShell.tsx` (tab + provider), new `components/TasksPanel.tsx`, small additions to `FeedColumn.tsx`, `IntercomPanel.tsx`, `CloudFilePicker.tsx`, `lib/api.ts`.
- Tests: Rust unit tests in `tasks.rs`; `tests/00-static/check_tasks.py` + mutations in `check_mutations.py` wired into `tests/run.py`; vitest for panel/bridge/consumers.
- Docs: `docs/ARC42.md` §7, `docs/VALIDATION.md`. No `contracts/tiles.yml` change (portal-native tab, not a service).
- Out of scope (follow-up deltas): landing widget "meine offenen Aufgaben", cloud→task share-link minting, task comments, cross-search.
