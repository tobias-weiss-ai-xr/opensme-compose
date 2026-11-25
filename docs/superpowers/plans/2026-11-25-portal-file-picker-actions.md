# Portal File-Picker Actions (D1–D5 / B3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the existing `cloud.rs` directory-create and file-delete methods through session-protected HTTP routes and make `CloudFileBrowser` able to use them.

**Architecture:** Two new axum routes on the existing protected router delegate to `cloud.for_user(session.sub)`; the Next.js catch-all `/api/*` proxy already forwards method/body/cookie verbatim, so no frontend proxy change is needed. A new Layer-0 static check pins route registration, firewall order and client usage; vitest tests pin the component's fetch calls.

**Tech Stack:** Rust/axum 0.7 portal, Next.js/React + vitest frontend, Python Layer-0 static checks.

**Build policy:** Never run `cargo build`/`cargo test` locally. Remote build host `vhrz2392` is currently unreachable (`Could not resolve hostname`), so Rust changes are verified by `cargo fmt --check` locally plus the static guard; CI verifies compilation after push. Run `ssh vhrz2392 'cd ~/git/openSME-compose/portal && cargo test'` if the host becomes reachable.

---

### Task 1: Layer-0 guard for the cloud bridge (RED)

**Files:**
- Create: `tests/00-static/check_cloud_bridge.py`
- Modify: `tests/run.py:62-64` (register the new check after the component bridge check)

- [x] **Step 1: Write the failing static check**

Create `tests/00-static/check_cloud_bridge.py` with exactly:

```python
#!/usr/bin/env python3
"""Layer 0 pins for portal cloud-file bridge mutations (D1-D5/B3).

C1 — mkdir and path-delete routes are registered.
C2 — both routes are inside the session firewall.
C3 — handlers validate the caller path and scope to the session user.
C4 — all four cloud error helpers are used (no raw upstream leaks).
C5 — the Next.js catch-all proxy forwards cookies and raw bodies.
C6 — CloudFileBrowser calls both endpoints with encoded paths.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

MAIN = ROOT / "portal" / "src" / "main.rs"
WEB = ROOT / "portal" / "web" / "src" / "components" / "CloudFileBrowser.tsx"
PROXY = ROOT / "portal" / "web" / "src" / "app" / "api" / "[[...path]]" / "route.ts"


def main():
    result = Result("cloud-bridge")
    result.header("Layer 0: portal cloud-file bridge validation")
    main = MAIN.read_text() if MAIN.exists() else ""
    web = WEB.read_text() if WEB.exists() else ""
    proxy = PROXY.read_text() if PROXY.exists() else ""

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)

    mkdir_route = '.route("/api/cloud/files/mkdir", post(handle_cloud_mkdir_post))'
    delete_route = '.route("/api/cloud/files/*path", delete(handle_cloud_delete_file))'
    check(mkdir_route in main, "C1 mkdir route registered")
    check(delete_route in main, "C1 path-delete route registered")

    firewall = main.find("session::require_session")
    for label, needle in (("mkdir", mkdir_route), ("delete", delete_route)):
        pos = main.find(needle)
        check(pos >= 0 and firewall >= 0 and pos < firewall,
              f"C2 {label} route is inside the session firewall")

    for name in ("handle_cloud_mkdir_post", "handle_cloud_delete_file"):
        start = main.find(f"async fn {name}")
        body = main[start:start + 1100] if start >= 0 else ""
        check("cloud::validate_path" in body,
              f"C3 {name} validates the caller path")
        check("cloud.for_user(session.sub.clone())" in body,
              f"C3 {name} scopes the DAV client to the session user")

    for helper in ("cloud_bad", "cloud_error", "cloud_mkdir_error", "cloud_not_configured"):
        check(helper in main, f"C4 {helper} used for error mapping")

    check(all(t in proxy for t in ('"cookie"', "req.arrayBuffer()", "forwardToBackend")),
          "C5 catch-all proxy forwards cookie and raw body")
    check(all(t in web for t in ("/api/cloud/files/mkdir", "/api/cloud/files/",
                                 "encodeURIComponent")),
          "C6 browser calls mkdir and path-delete with encoded paths")

    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [x] **Step 2: Register it and verify RED**

Edit `tests/run.py`, after the `Portal component bridge (intercom)` tuple add:

```python
            ("Portal cloud-file bridge (contract)", "tests/00-static/check_cloud_bridge.py"),
```

Run: `python3 tests/00-static/check_cloud_bridge.py`
Expected: FAIL with C1/C2/C3/C6 findings (C4/C5 already pass because the helpers and proxy exist).

- [x] **Step 3: Commit the failing guard**

```bash
git add tests/00-static/check_cloud_bridge.py tests/run.py
git commit -m "test(layer0): pin portal cloud-file bridge routes"
```

---

### Task 2: Rust routes and handlers (GREEN for C1–C4)

**Files:**
- Modify: `portal/src/main.rs:1113-1130` (router registration), `portal/src/main.rs:1630-1640` (`CloudMkdirBody`), `portal/src/main.rs:1860-1880` (handlers)

- [x] **Step 1: Register the routes**

Replace the cloud-bridge route block in `build_router` with:

```rust
        .route(
            "/api/cloud/files",
            get(handle_cloud_list)
                .post(handle_cloud_mkdir)
                .put(handle_cloud_upload),
        )
        .route("/api/cloud/files/metadata", get(handle_cloud_list_files))
        .route(
            "/api/cloud/files/mkdir",
            post(handle_cloud_mkdir_post),
        )
        .route(
            "/api/cloud/files/*path",
            delete(handle_cloud_delete_file),
        )
        .route(
            "/api/cloud/file",
            get(handle_cloud_download).delete(handle_cloud_delete),
        )
```

Keep this block before `session::require_session` is layered, exactly as the existing cloud routes are.

- [x] **Step 2: Add the request body type**

Insert immediately after `struct CloudCreateQuery { ... }`:

```rust
/// JSON body for POST /api/cloud/files/mkdir.
#[derive(Deserialize)]
struct CloudMkdirBody {
    path: String,
}
```

- [x] **Step 3: Add the two handlers**

Insert immediately before `/// POST /api/cloud/share?path=<file>`:

```rust
/// POST /api/cloud/files/mkdir — create a directory via MKCOL. Requires a
/// session; the DAV client is scoped to the signed-in user. 201 on success.
async fn handle_cloud_mkdir_post(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    Json(body): Json<CloudMkdirBody>,
) -> impl IntoResponse {
    let path = match cloud::validate_path(&body.path) {
        Ok(p) => p,
        Err(e) => return cloud_bad(&e),
    };
    let Some(cloud) = &config.cloud else {
        return cloud_not_configured();
    };
    let user_cloud = cloud.for_user(session.sub.clone());
    match user_cloud.create_directory(&path).await {
        Ok(()) => StatusCode::CREATED.into_response(),
        Err(e) => cloud_mkdir_error(&e),
    }
}

/// DELETE /api/cloud/files/*path — delete a file via DELETE. Requires a
/// session; the DAV client is scoped to the signed-in user. 204 on success.
async fn handle_cloud_delete_file(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    axum::extract::Path(path): axum::extract::Path<String>,
) -> impl IntoResponse {
    let path = match cloud::validate_path(&path) {
        Ok(p) => p,
        Err(e) => return cloud_bad(&e),
    };
    let Some(cloud) = &config.cloud else {
        return cloud_not_configured();
    };
    let user_cloud = cloud.for_user(session.sub.clone());
    match user_cloud.delete_file(&path).await {
        Ok(()) => StatusCode::NO_CONTENT.into_response(),
        Err(e) => cloud_error(&e),
    }
}
```

- [x] **Step 4: Add Rust contract tests**

Append inside `mod tests` (before the final `}`), using the existing `respond`/`respond_anon` helpers:

```rust
    #[tokio::test]
    async fn cloud_mkdir_and_delete_require_a_session() {
        let cfg = cfg_with("", None, "");
        assert_eq!(
            respond_anon(cfg, "POST", "/api/cloud/files/mkdir").await.status(),
            axum::http::StatusCode::UNAUTHORIZED
        );
        let cfg = cfg_with("", None, "");
        assert_eq!(
            respond_anon(cfg, "DELETE", "/api/cloud/files/note.txt").await.status(),
            axum::http::StatusCode::UNAUTHORIZED
        );
    }

    #[tokio::test]
    async fn cloud_mkdir_rejects_traversal_before_touching_the_backend() {
        let cfg = cfg_with("", None, "");
        let res = respond(
            cfg,
            "POST",
            "/api/cloud/files/mkdir",
            Some(serde_json::json!({"path": "../escape"})),
        )
        .await;
        assert_eq!(res.status(), axum::http::StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn cloud_routes_report_503_without_webdav_config() {
        let cfg = cfg_with("", None, "");
        let res = respond(
            cfg,
            "POST",
            "/api/cloud/files/mkdir",
            Some(serde_json::json!({"path": "notes"})),
        )
        .await;
        assert_eq!(res.status(), axum::http::StatusCode::SERVICE_UNAVAILABLE);
        let cfg = cfg_with("", None, "");
        let res = respond(cfg, "DELETE", "/api/cloud/files/note.txt", None).await;
        assert_eq!(res.status(), axum::http::StatusCode::SERVICE_UNAVAILABLE);
    }
```

- [x] **Step 5: Format and verify the static guard**

Run: `cd portal && cargo fmt --check`
Expected: no diff (or run `cargo fmt` first if rustfmt reports changes — formatting only, no build).

Run: `python3 tests/00-static/check_cloud_bridge.py`
Expected: C1–C4 PASS; C6 still FAIL.

If `ssh vhrz2392` resolves again, run:
`ssh vhrz2392 'cd ~/git/openSME-compose/portal && cargo test'`
Expected: all tests pass, including the three new ones.

- [x] **Step 6: Commit**

```bash
git add portal/src/main.rs
git commit -m "feat(portal): expose cloud mkdir and path-delete bridge routes"
```

---

### Task 3: Browser actions and vitest coverage (GREEN for C6)

**Files:**
- Modify: `portal/web/src/components/CloudFileBrowser.tsx:36-80` (built-in default actions)
- Create: `portal/web/src/components/CloudFileBrowser.test.tsx`

- [x] **Step 1: Write the failing frontend tests**

Create `portal/web/src/components/CloudFileBrowser.test.tsx`:

```tsx
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import CloudFileBrowser, { type CloudFile } from "./CloudFileBrowser";

const FILES = [
  { name: "report.pdf", path: "report.pdf", size: 12, is_dir: false,
    modified_at: null, type: "file" as const },
];

const listing = () =>
  Promise.resolve(new Response(JSON.stringify({ files: FILES, path: "" }), {
    status: 200, headers: { "content-type": "application/json" },
  }));

describe("CloudFileBrowser built-in actions", () => {
  beforeEach(() => { vi.restoreAllMocks(); });

  it("creates a folder via POST /api/cloud/files/mkdir", async () => {
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => listing())       // initial load
      .mockImplementationOnce((url: string, init: RequestInit) => {
        expect(url).toBe("/api/cloud/files/mkdir");
        expect(init.method).toBe("POST");
        expect(JSON.parse(String(init.body))).toEqual({ path: "notes" });
        return Promise.resolve(new Response(null, { status: 201 }));
      })
      .mockImplementationOnce(() => listing());       // reload
    vi.stubGlobal("fetch", fetchMock);

    render(<CloudFileBrowser />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /new folder/i }));
    fireEvent.change(screen.getByPlaceholderText(/enter folder name/i), {
      target: { value: "notes" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("deletes a file via DELETE /api/cloud/files/<encoded path>", async () => {
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => listing())
      .mockImplementationOnce((url: string, init: RequestInit) => {
        expect(url).toBe("/api/cloud/files/report.pdf");
        expect(init.method).toBe("DELETE");
        return Promise.resolve(new Response(null, { status: 204 }));
      })
      .mockImplementationOnce(() => listing());
    vi.stubGlobal("fetch", fetchMock);

    render(<CloudFileBrowser />);
    await waitFor(() => expect(screen.getByText("report.pdf")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /^delete/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("shows the server error when mkdir fails", async () => {
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => listing())
      .mockImplementationOnce(() => Promise.resolve(
        new Response(JSON.stringify({ error: "directory already exists" }),
          { status: 409, headers: { "content-type": "application/json" } })));
    vi.stubGlobal("fetch", fetchMock);

    render(<CloudFileBrowser />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /new folder/i }));
    fireEvent.change(screen.getByPlaceholderText(/enter folder name/i), {
      target: { value: "notes" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));
    await waitFor(() =>
      expect(screen.getByText(/directory already exists/i)).toBeTruthy());
  });
});
```

- [x] **Step 2: Run tests to verify RED**

Run: `cd portal/web && bun run test --run src/components/CloudFileBrowser.test.tsx`
Expected: FAIL — the browser currently disables both buttons when no callbacks are passed, so no requests are made.

- [x] **Step 3: Add built-in default actions**

In `CloudFileBrowser.tsx`, add these two helpers after `list_files`:

```tsx
/** Built-in folder creation (used when no parent supplies onCreateDir). */
async function create_dir(path: string): Promise<void> {
  const res = await fetch("/api/cloud/files/mkdir", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) {
    const data = (await res.json().catch(() => null)) as { error?: string } | null;
    throw new Error(data?.error ?? `HTTP ${res.status}`);
  }
}

/** Built-in file delete (used when no parent supplies onDelete). */
async function delete_files(items: CloudFile[]): Promise<void> {
  for (const item of items) {
    const segments = item.path.split("/").filter(Boolean).map(encodeURIComponent);
    const res = await fetch(`/api/cloud/files/${segments.join("/")}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      const data = (await res.json().catch(() => null)) as { error?: string } | null;
      throw new Error(data?.error ?? `HTTP ${res.status}`);
    }
  }
}
```

Change `handleCreateFolder` to call the built-in when no callback is supplied:

```tsx
  const handleCreateFolder = useCallback(async () => {
    const name = newFolderName.trim();
    if (!name) return;
    const fullPath = currentPath === "/" ? name : `${currentPath.replace(/\/$/, "")}/${name}`;
    try {
      if (onCreateDir) await onCreateDir(name);
      else await create_dir(fullPath);
      setNewFolderName("");
      setShowNewFolder(false);
      await loadFiles(currentPath);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [currentPath, loadFiles, newFolderName, onCreateDir]);
```

Change `handleDelete` similarly:

```tsx
  const handleDelete = useCallback(async () => {
    const selectedItems = selectedFiles();
    if (selectedItems.length === 0) return;
    try {
      if (onDelete) await onDelete(selectedItems);
      else await delete_files(selectedItems);
      setSelected(new Set());
      await loadFiles(currentPath);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [currentPath, loadFiles, onDelete, selectedFiles]);
```

Change the two `disabled=` props so the built-in actions enable the buttons:

- New Folder button (line ~315): remove `disabled={!onCreateDir}` entirely.
- Delete button (line ~322): replace `disabled={selected.size === 0 || !onDelete}` with `disabled={selected.size === 0}`.

Keep the existing English UI copy (`New Folder`, `Create`, placeholder
`Enter folder name`, `Delete (n)`) unchanged; the tests use those exact labels.

Because `CloudFileBrowser` currently has no consumer in `portal/web`, the
built-in fetch actions are required — callback-only wiring would leave both
buttons permanently disabled. Their optional callback props stay intact for
future embedders.

- [x] **Step 4: Run tests to verify GREEN**

Run: `cd portal/web && bun run test --run src/components/CloudFileBrowser.test.tsx`
Expected: 3 passed, 0 failed.

- [x] **Step 5: Commit**

```bash
git add portal/web/src/components/CloudFileBrowser.tsx portal/web/src/components/CloudFileBrowser.test.tsx
git commit -m "feat(portal-web): built-in mkdir/delete actions in cloud browser"
```

---

### Task 4: Full gate and backlog status

**Files:**
- Modify: `docs/OPENCLOUD-CAMPAIGNS-SUMMARY.md:249-258` (check off A2, D1–D5, B3 with evidence)

- [x] **Step 1: Run the complete static gate**

Run: `python3 tests/run.py --static`
Expected: 31 suites pass (the new cloud-bridge suite included), 0 failures.

- [x] **Step 2: Mark verified backlog items**

In `docs/OPENCLOUD-CAMPAIGNS-SUMMARY.md`, change only these lines:

```markdown
- [x] **A2**: Add S3 storage support to Paperless-ngx (already wired via `PAPERLESS_AWS_*` in `services/paperless.yml`)
- [x] **B3**: Multi-user file picker in portal (`cloud.for_user` + session-scoped bridge routes)
- [x] **D1-D5**: Complete portal file picker enhancements (metadata listing, OCS share management, mkdir + path-delete via `POST /api/cloud/files/mkdir` and `DELETE /api/cloud/files/*path`)
```

Leave B1/B2/C2/C3/A1/E2 unchecked — they are separate sub-projects.

- [x] **Step 3: Final commit**

```bash
git add docs/OPENCLOUD-CAMPAIGNS-SUMMARY.md
git commit -m "docs(backlog): close cloud file-picker and Paperless S3 items"
```

---

## Self-review notes

- Spec coverage: routes (Task 2), session/DAV scoping (Task 2 + Task 1 C2/C3), error mapping (Task 2, pinned C4), UI (Task 3), testing (all tasks), no new service (architecture), remote build policy (header).
- Placeholder scan: none; every code step contains complete code.
- Type consistency: `CloudMkdirBody.path`, handler names, route strings and fetch URLs are identical in every task and in the static guard.
