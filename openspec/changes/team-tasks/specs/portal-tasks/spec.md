# portal-tasks Specification

## Purpose

Defines the portal-native team task list: an SME team can turn any portal
content (news article, team note, cloud file) into an accountable task with
assignee, due date and priority, tracked in one Aufgaben tab behind the
session firewall — without a new service or tile.

## ADDED Requirements

### Requirement: Task model and API behind the session firewall

The portal SHALL expose task CRUD endpoints (`GET /api/tasks`,
`POST /api/tasks`, `PATCH /api/tasks/:id`, `DELETE /api/tasks/:id`) on the
protected router, so anonymous callers receive 401. A task SHALL carry
`id`, `title` (1–200 chars), optional `description`, `assignee` (non-empty,
≤100 chars), `author` (stamped from the session, never client-supplied),
`priority` (`low|normal|high`, default `normal`), optional `due`
(`YYYY-MM-DD`), `status` (`open|done`), optional `source_url` (http(s) only),
`created_at` and `done_at?`. `GET /api/tasks` SHALL support
`filter=all|mine|open|done` and `since=<id>` delta queries.

#### Scenario: Anonymous caller is rejected
- **WHEN** an anonymous request hits any `/api/tasks` route
- **THEN** the response is 401 and no task state changes

#### Scenario: Create stamps the author from the session
- **WHEN** a signed-in member posts a task without an author field
- **THEN** the stored task carries the session user's name/email as author

#### Scenario: Invalid input is a clean 400
- **WHEN** a task is posted with an empty title, a bad due format, or an unknown priority
- **THEN** the API returns 400 with a German error message and stores nothing

#### Scenario: Delta listing
- **WHEN** `GET /api/tasks?since=42` is requested
- **THEN** only tasks with id > 42 are returned

### Requirement: Persistence with a bounded store

The task store SHALL persist write-through to a JSON file when
`TASKS_DATA_PATH` is set (atomic tmp+rename, reload on boot) and SHALL run
in-memory otherwise. The store SHALL be capped at 500 tasks, evicting the
oldest **done** task first; open tasks SHALL never be evicted by the cap.

#### Scenario: Store survives a restart
- **WHEN** tasks exist and the portal restarts with `TASKS_DATA_PATH` set
- **THEN** `GET /api/tasks` returns the same tasks with the same ids

#### Scenario: Cap evicts oldest done only
- **WHEN** the store reaches 500 tasks and a new task is created
- **THEN** the oldest done task is removed and all open tasks remain

### Requirement: Completion and deletion are restricted to assignee or author

Any signed-in member MAY create and (re)assign tasks. Only the task's
assignee or its author (matched case-insensitively against the session email
or name) SHALL be allowed to set `status=done` or delete the task; other
members SHALL receive 403.

#### Scenario: Assignee marks own task done
- **WHEN** the assignee PATCHes `{"status":"done"}`
- **THEN** the task becomes done and `done_at` is set

#### Scenario: Unrelated member cannot complete
- **WHEN** a member who is neither assignee nor author PATCHes `{"status":"done"}`
- **THEN** the response is 403 and the task stays open

#### Scenario: Author deletes, unrelated member cannot
- **WHEN** the author DELETEs the task
- **THEN** it is removed; a DELETE by an unrelated member returns 403

### Requirement: Known team members endpoint

`GET /api/tasks/users` SHALL return the distinct, sorted list of member names
and emails already visible in team notes (authors) and tasks
(authors/assignees), so the assignee picker can offer them as a dropdown while
still accepting free text. It SHALL NOT call the IdM.

#### Scenario: Dropdown is seeded from existing activity
- **WHEN** notes and tasks exist with authors "anna@…" and "bob@…"
- **THEN** `GET /api/tasks/users` lists both, sorted, deduplicated

#### Scenario: Empty on a fresh install
- **WHEN** no notes or tasks exist yet
- **THEN** the endpoint returns an empty list and the UI falls back to free text

### Requirement: Bridge slot for creating tasks from portal content

The frontend bridge SHALL expose a typed `task.create` slot
(`(task: { title: string; url?: string }) => void`). The portal shell SHALL
register it: activating it switches to the Aufgaben tab and opens the create
form prefilled with the given title and optional source link. Feed articles,
team notes and cloud-file picker rows SHALL each offer an "Als Aufgabe anlegen"
action that calls the slot; when no provider is registered, no such affordance
SHALL be rendered (fail-soft, like the other slots).

#### Scenario: Feed article becomes a task
- **WHEN** a feed card's "Als Aufgabe anlegen" is activated
- **THEN** the Aufgaben tab opens with the form prefilled with the article title and link, and saving creates the task via `POST /api/tasks`

#### Scenario: Note becomes a task
- **WHEN** a note's "Als Aufgabe anlegen" is activated
- **THEN** the form is prefilled with a title derived from the note text

#### Scenario: Cloud file becomes a task
- **WHEN** a cloud picker row's "Als Aufgabe anlegen" is activated
- **THEN** the form is prefilled with the file name and the cloud path in the description

#### Scenario: No provider registered
- **WHEN** no `task.create` provider is registered
- **THEN** no "Als Aufgabe" button is rendered anywhere and all hosts stay fully functional

### Requirement: Aufgaben tab UI

The portal shell SHALL render an "Aufgaben" tab containing the task panel:
a create form (title, assignee dropdown + free text, optional due date,
priority, optional description/link), a filter (alle / meine / offen /
erledigt), a list sorted by due date then priority with overdue highlighting,
and per-task done-toggle and delete actions. The panel SHALL poll
`GET /api/tasks?since=` for updates. A 403 from a restricted action SHALL show
an inline error without losing form content.

#### Scenario: Filter shows my open tasks
- **WHEN** the filter is "meine" + "offen"
- **THEN** only open tasks whose assignee matches the session user are listed

#### Scenario: Overdue task is highlighted
- **WHEN** an open task's due date is in the past
- **THEN** the list row is visually marked as overdue

#### Scenario: Restricted action shows inline 403
- **WHEN** a member tries to complete a task they may not complete
- **THEN** an inline error is shown and the task list is unchanged
