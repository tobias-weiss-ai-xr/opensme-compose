# Tasks: team-tasks

## 1. Backend: tasks.rs Store + Validierung (RED zuerst)

- [x] {n} `portal/src/tasks.rs` (RED): Unit-Tests — Task-Modell (Serde),
      Validierung (Titel 1–200, due `YYYY-MM-DD`, Priority-Enum,
      Assignee ≤100 nicht-leer, source_url nur http(s)), Cap 500 evictet
      älteste *done* zuerst (nie open), JSON-Roundtrip, leere-Datei /
      kaputte Datei laden → leerer Store
- [x] {n} `portal/src/tasks.rs` (GREEN): `TaskStore` geklont aus dem
      Intercom-Pattern — `TASKS_DATA_PATH` (atomic tmp+rename, reload on
      boot), in-memory wenn unset, Cap-Eviction, `list(filter, since)`,
      `create`, `patch`, `delete`; Permissions-Hilfsfn
      `may_complete(session, task)` (assignee ODER author, case-insensitive
      gegen session email/name) — Tests grün (`cargo test tasks`)
- [x] {n} `portal/src/main.rs`: Routen im protected router —
      `GET/POST /api/tasks`, `PATCH/DELETE /api/tasks/:id`,
      `GET /api/tasks/users` (distinct aus intercom-Autoren + task
      author/assignee, sortiert); Handler stampen author aus
      `Extension<Session>`, 403 bei may_complete-Verstoß, 400 bei
      Validierung; Store-Wiring in AppConfig wie intercom

## 2. Layer-0 Static Guards

- [x] {n} `tests/00-static/check_tasks.py` (RED→GREEN): parst `main.rs`
      (alle 5 Routen im protected-Block), `tasks.rs` (Cap-Konstante,
      Eviction done-only), prüft `TASKS_DATA_PATH`; wired in
      `tests/run.py`
- [x] {n} `tests/00-static/check_mutations.py`: +4 Mutationen
      (Route entfernt / 403-Check entfernt / Cap evictet open / author
      client-supplied) — Word-Boundary-Regex, `old` exakt 1×

## 3. Frontend: Bridge-Slot + Aufgaben-Tab

- [x] {n} `lib/bridge.ts` + `bridge.test.ts` (RED→GREEN): neuer Slot
      `"task.create"`: `(task: { title: string; url?: string }) => void`,
      SSR-sicher (getServerSnapshot), fail-soft; Slot-Typ parst
- [x] {n} `lib/api.ts`: `listTasks(filter, since)`, `createTask`,
      `patchTask`, `deleteTask`, `listTaskUsers` + Unit-Tests (fetch-stub)
- [x] {n} `components/TasksPanel.tsx` + Test (RED→GREEN): Formular
      (Titel, Assignee-Dropdown aus /api/tasks/users + Freitext, due,
      priority, optionale Beschreibung/Link), Filter
      alle/meine/offen/erledigt, Sortierung due→priority, Overdue-
      Highlight, done-toggle + delete mit inline-403 (Formularzustand
      bleibt), Polling `?since=` wie IntercomPanel
- [x] {n} `components/PortalShell.tsx` + Test: neuer Tab "Aufgaben" mit
      `<TasksPanel/>`, `task.create` registriert → Tab-Wechsel + Prefill
      (gleiches Muster wie `share.open`)

## 4. Konsumenten: "Als Aufgabe anlegen" (Q5-C)

- [x] {n} `FeedColumn.tsx` + Test: pro Artikel "Als Aufgabe" →
      `task.create({ title: article.title, url: article.link })`; ohne
      Provider kein Button
- [x] {n} `IntercomPanel.tsx` + Test: pro Notiz "Als Aufgabe" →
      `task.create({ title: excerpt })`
- [x] {n} `CloudFilePicker.tsx` + Test: pro Datei-Zeile "Als Aufgabe" →
      `task.create({ title: name })` + Pfad in description
- [x] {n} `check_bridge.py` erweitern: `task.create` Slot + 3 Konsumenten
      zählen

## 5. Gates, Docs, Live-Verify

- [x] {n} Volle Gates lokal: `cargo test` (portal), `bunx vitest run`
      (web), eslint (Baseline 2 warnings), `bash tests/00-static/run.sh`
      (Layer 0 inkl. Mutationen), `openspec validate --changes`
- [x] {n} Docs: `docs/ARC42.md` §7, `docs/VALIDATION.md` aktualisieren;
      Commit(s) nach Konvention (R23-Präfix)
- [x] 5.3 VPS-Deploy (buildx --network host) + Smoke (`docker exec`
      health, `/` 200); Live-Playwright: Aufgabe aus Feed anlegen →
      in Liste; Notiz → Aufgabe; Cloud-Datei → Aufgabe; 403-Pfad
      (anderer User darf nicht erledigen); Filter/Sortierung
