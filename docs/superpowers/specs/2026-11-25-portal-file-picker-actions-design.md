# Design: Portal file-picker actions (D1–D5 / B3)

Date: 2026-11-25
Status: design approved by user; awaiting written-spec review

## Context and goal

`docs/OPENCLOUD-CAMPAIGNS-SUMMARY.md` lists D1–D5 (portal file-picker
enhancements: listing, share management) and B3 (multi-user file picker) as
still open. In reality `portal/src/cloud.rs` already provides the multi-user
client (`for_user`, `user_dav_path`), rich `list_files`, OCS share
management, `create_directory` and `delete_file`; the Rust bridge exposes
list, metadata, upload, download, query-style delete and share. What is
missing is HTTP exposure of directory creation and path-based file deletion,
plus picker UI for those actions. The abandoned `tf/T-OCC4-010` branch added
the two routes without tests; this spec reimplements them cleanly.

Goal: a signed-in user can create a folder and delete a file from the portal
cloud surfaces without leaving the app, with the same session, tenancy and
error semantics as the existing bridge.

## Decisions

| Decision | Choice |
|---|---|
| Implementation approach | Reimplement the two routes with tests (not cherry-pick the abandoned attempt) |
| Routes | `POST /api/cloud/files/mkdir` (JSON `{path}`), `DELETE /api/cloud/files/*path` |
| Auth/tenancy | Existing session firewall + `cloud.for_user(session.sub.clone())` |
| Error mapping | Reuse `cloud_bad`, `cloud_error`, `cloud_mkdir_error`, `cloud_not_configured` |
| UI scope | Wire the existing `CloudFileBrowser` callbacks; keep `CloudFilePicker` read/share-only |
| Backend build | Remote host `vhrz2392` only (per `AGENTS.md`); no local `cargo build` |

## Architecture

```
browser (CloudFileBrowser)
  POST /api/cloud/files/mkdir   {path}  ─┐
  DELETE /api/cloud/files/<path>        ─┼─> Next.js catch-all route handler
                                         │    (src/app/api/[[...path]]/route.ts)
                                         │       │
                                         │       v
                                         │  Rust portal: session firewall
                                         │       │
                                         │       v
                                         │  cloud.for_user(sub) -> MKCOL / DELETE
                                         │       │
                                         └───────┘ 201 / 204 or mapped 4xx/502/503
```

No new service, port or credentials are introduced. The browser never sees
WebDAV credentials; every request is same-origin and session-gated.

## Components

### `portal/src/main.rs`

- Register both routes on the existing protected router, next to
  `/api/cloud/files/metadata`, so the session firewall applies.
- `CloudMkdirBody { path: String }` — deserialized JSON body; path goes
  through `cloud::validate_path` before any DAV call.
- `handle_cloud_mkdir_post`: `cloud_dir_or_root`-style validation → 400 on
  invalid path, 503 when cloud is unconfigured, 201 Created on success,
  `cloud_mkdir_error` on failure (405 → 409 "directory already exists").
- `handle_cloud_delete_file`: wildcard path validation → 400 invalid, 503
  unconfigured, 204 No Content on success, `cloud_error` on failure.
  Wildcard matching must not overlap `/api/cloud/files/metadata` (axum picks
  the more specific static route, and that route stays untouched).

### `portal/web/src/components/CloudFileBrowser.tsx`

- Implement the existing `onCreateDir` and `onDelete` callbacks with
  `POST /api/cloud/files/mkdir` and `DELETE /api/cloud/files/<path>`;
  refresh the listing after success and surface errors in the existing
  error state. Paths are URL-encoded per segment (`encodeURIComponent`).
- No change to `CloudFilePicker`: its role is select + share, not mutation.

### Tests

- Rust unit tests: path validation rejects traversal/absolute paths; error
  mapping produces 400/409/502/503 as specified.
- New `tests/00-static/check_cloud_bridge.py`, registered in
  `tests/run.py` Layer 0: assert both routes are registered on the
  protected router, that the session firewall precedes them, and that the
  catch-all Next.js route forwards cookies rather than constructing a
  cross-origin request.
- `portal/web/src/components/CloudFileBrowser.test.tsx`: mock fetch for
  mkdir/delete, assert request URLs/methods, reload on success, error state
  on failure.

## Data flow and error handling

1. Browser sends a relative cloud path; client encodes it.
2. Portal validates with `cloud::validate_path` (rejects traversal and
   malformed paths) before constructing a DAV URL.
3. `cloud::CloudFiles::for_user(session.sub)` scopes the request to the
   signed-in user's DAV subtree — no caller-supplied username is trusted.
4. Success: `201` (mkdir) or `204` (delete) with no body.
5. Failure: `cloud_mkdir_error`/`cloud_error` map upstream status through
   `cloud::http_status`: 404 → 404, 405/409/423 → 409, other 4xx → 400,
   5xx/transport → 502; missing config → 503.

## Out of scope

- Recursive directory delete, rename/move, overwrite confirmation.
- Cross-user administration, quota enforcement, trash/undo.
- Changes to the legacy Rust `/files` page beyond the shared bridge routes.
- WebDAV archival for Paperless/Invoice Ninja (separate sub-project).

## Verification

- `ssh vhrz2392 'cd <src> && cargo test'` for portal Rust tests.
- `cd portal/web && bun run test --run` for frontend tests.
- `python3 tests/run.py --static` → 30 suites green.
- Manual smoke on a deployment with OpenCloud wired: create folder, delete
  file, verify anonymous requests get 401 and unconfigured cloud gets 503.
