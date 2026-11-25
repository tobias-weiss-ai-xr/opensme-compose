# Suite Event Producers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** publish actor-scoped cloud and SSO lifecycle events from the portal into the existing Redis event stream.

**Architecture:** the four cloud bridge handlers and the OIDC callback/logout handlers call the existing `EventBus::publish` with `Visibility::Recipients([actor])`, fail-soft; a new Layer-0 static check pins every producer and its payload shape.

**Tech Stack:** Rust/axum (`portal/src/main.rs`, `session.rs`, `logout.rs`, `events.rs`), Python Layer-0 checks, Next.js SSE dispatcher unchanged.

---

### Task 1: RED — pin the producer contract in Layer 0

**Files:**
- Create: `tests/00-static/check_event_producers.py`
- Modify: `tests/run.py`, `tests/00-static/check_mutations.py`, `docs/VALIDATION.md`

- [ ] **Step 1: create the failing check**

```python
#!/usr/bin/env python3
"""Layer 0 pins for cloud/SSO event producers (realtime-events slice 1)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

PORTAL = ROOT / "portal" / "src"
MAIN = (PORTAL / "main.rs").read_text()
SESSION = (PORTAL / "session.rs").read_text()
LOGOUT = (PORTAL / "logout.rs").read_text()


def handler_block(src: str, fn: str) -> str:
    start = src.index(fn)
    nxt = src.find("\nasync fn ", start + 1)
    return src[start: nxt if nxt > 0 else len(src)]


def main():
    result = Result("event-producers")
    result.header("Layer 0: cloud and session event producers")

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)

    cloud = {
        "handle_cloud_upload": ("cloud.uploaded", r'\{ "path": full, "name": name \}'),
        "handle_cloud_mkdir_post": ("cloud.mkdir", r'\{ "path": path \}'),
        "handle_cloud_delete_file": ("cloud.deleted", r'\{ "path": path \}'),
        "handle_cloud_share": ("cloud.shared", r'\{ "path": path, "url": url \}'),
    }
    for fn, (kind, payload) in cloud.items():
        block = handler_block(MAIN, fn)
        check(kind in block and ".event_bus" in block and ".publish(&events::Event::new(" in block,
              f"P1 {fn} publishes {kind} via EventBus")
        check(re.search(payload, block) is not None,
              f"P2 {fn} payload carries only path/name/url metadata")
        check("events::Visibility::Recipients(vec![session.sub.clone()])" in block,
              f"P3 {fn} scopes the event to the actor")
        check("Visibility::Team" not in block,
              f"P3 {fn} never uses Team visibility")

    callback = handler_block(SESSION, "pub async fn handle_callback")
    check('"session.login"' in callback and ".event_bus" in callback
          and "events::Visibility::Recipients(vec![user.sub.clone()])" in callback,
          "P4 OIDC callback publishes actor-scoped session.login")
    check(re.search(r'session\.(login|logout)[\s\S]{0,300}Visibility::Team', SESSION) is None,
          "P4 session events are never team-wide")

    check("pub async fn handle_logout" in LOGOUT
          and '"session.logout"' in LOGOUT
          and "session::verify_session(&config.session, token)" in LOGOUT
          and "events::Visibility::Recipients(vec![sub.clone()])" in LOGOUT,
          "P5 logout verifies the portal session before publishing session.logout")
    check(re.search(r'cloud\.(uploaded|mkdir|deleted|shared)[\s\S]{0,300}Visibility::Team', MAIN) is None,
          "P5 cloud events are never team-wide")
    result.done()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: register the check**

In `tests/run.py`, after the `("Portal real-time events (contract)", "tests/00-static/check_events.py"),` line add:

```python
    ("Portal event producers (cloud + session)", "tests/00-static/check_event_producers.py"),
```

- [ ] **Step 3: add mutation teeth**

In `tests/00-static/check_mutations.py`, inside the `MUTATIONS` list, add:

```python
    {
        "name": "cloud-mkdir-publish-removed",
        "file": "portal/src/main.rs",
        "old": '            "cloud.mkdir",',
        "new": '            "cloud.mkdir-removed",',
        "check": "tests/00-static/check_event_producers.py",
        "why": "event-producers — removing the cloud.mkdir publish must fail "
               "the Layer-0 producer pin",
    },
```

- [ ] **Step 4: add the inventory line**

In `docs/VALIDATION.md`, after the `check_events.py` line add:

```markdown
- [x] `check_event_producers.py` — cloud + SSO event producers: actor-scoped
      pubs in upload/mkdir/delete/share, session.login callback, session.logout
      with verified cookie, no team-wide or byte-carrying payloads
```

- [ ] **Step 5: prove RED and commit**

Run: `python3 tests/run.py --static`
Expected: FAIL with `P1…P5` violations from `event-producers`.
Run: `git add tests/00-static/check_event_producers.py tests/run.py tests/00-static/check_mutations.py docs/VALIDATION.md && git commit -m "test(layer0): pin cloud and SSO event producers (RED)"`

### Task 2: cloud bridge producers

**Files:** `portal/src/main.rs`

- [ ] **Step 1: add session extractors to upload and share**

In `handle_cloud_upload`, insert after `headers: axum::http::HeaderMap,`:

```rust
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
```

In `handle_cloud_share`, insert after `axum::extract::Query(q): axum::extract::Query<CloudPathQuery>,` the same line. Both routes are behind `session::require_session`, which already inserts the extension.

- [ ] **Step 2: publish after each successful mutation**

In `handle_cloud_upload`, change the success arm to:

```rust
        Ok(()) => {
            let _ = config
                .event_bus
                .publish(&events::Event::new(
                    "cloud.uploaded",
                    &session.sub,
                    &config.opensme_domain,
                    events::Visibility::Recipients(vec![session.sub.clone()]),
                    serde_json::json!({ "path": full, "name": name }),
                ))
                .await;
            (
                StatusCode::CREATED,
                Json(serde_json::json!({ "path": full })),
            )
                .into_response()
        }
```

In `handle_cloud_mkdir_post`:

```rust
        Ok(()) => {
            let _ = config
                .event_bus
                .publish(&events::Event::new(
                    "cloud.mkdir",
                    &session.sub,
                    &config.opensme_domain,
                    events::Visibility::Recipients(vec![session.sub.clone()]),
                    serde_json::json!({ "path": path }),
                ))
                .await;
            StatusCode::CREATED.into_response()
        }
```

In `handle_cloud_delete_file`:

```rust
        Ok(()) => {
            let _ = config
                .event_bus
                .publish(&events::Event::new(
                    "cloud.deleted",
                    &session.sub,
                    &config.opensme_domain,
                    events::Visibility::Recipients(vec![session.sub.clone()]),
                    serde_json::json!({ "path": path }),
                ))
                .await;
            StatusCode::NO_CONTENT.into_response()
        }
```

In `handle_cloud_share`:

```rust
        Ok(url) => {
            let _ = config
                .event_bus
                .publish(&events::Event::new(
                    "cloud.shared",
                    &session.sub,
                    &config.opensme_domain,
                    events::Visibility::Recipients(vec![session.sub.clone()]),
                    serde_json::json!({ "path": path, "url": url }),
                ))
                .await;
            (StatusCode::CREATED, Json(serde_json::json!({ "url": url }))).into_response()
        }
```

- [ ] **Step 3: add a Rust unit test**

In the existing `#[cfg(test)]` module in `portal/src/main.rs`, append:

```rust
    #[tokio::test]
    async fn cloud_event_payload_stays_actor_scoped() {
        let bus = events::EventBus::memory();
        let e = events::Event::new(
            "cloud.uploaded",
            "alice",
            "example.org",
            events::Visibility::Recipients(vec!["alice".into()]),
            serde_json::json!({ "path": "notes/a.txt", "name": "a.txt" }),
        );
        bus.publish(&e).await.unwrap();
        let all = bus.replay(None).await.unwrap();
        assert_eq!(all[0].kind, "cloud.uploaded");
        assert_eq!(all[0].payload["path"], "notes/a.txt");
        assert!(!all[0].visibility.visible_to(&events::Viewer {
            sub: "bob".into(),
            email: "".into(),
            name: "".into(),
            tenant: "example.org".into(),
        }));
    }
```

- [ ] **Step 4: verify and commit**

Run: `python3 tests/run.py --static`
Expected: cloud P1–P3 now pass; session P4/P5 still fail.
Run: `cargo fmt --all -- --check` (local formatting only; no local build — AGENTS.md).
Run: `git add portal/src/main.rs && git commit -m "feat(events): publish actor-scoped cloud file and share events"`

### Task 3: SSO lifecycle producers

**Files:** `portal/src/session.rs`, `portal/src/logout.rs`

- [ ] **Step 1: publish session.login in the callback**

In `portal/src/session.rs`, in `handle_callback`'s `Ok((user, id_token)) =>` arm, after `let cookie = mint_session(&config.session, &user.sub, &user.name, &user.email);` insert:

```rust
            let _ = config
                .event_bus
                .publish(&crate::events::Event::new(
                    "session.login",
                    &user.sub,
                    &config.opensme_domain,
                    crate::events::Visibility::Recipients(vec![user.sub.clone()]),
                    serde_json::json!({}),
                ))
                .await;
```

- [ ] **Step 2: publish session.logout after session verification**

In `portal/src/logout.rs`, insert after `let id_token = cookie_value(&headers, SESSION_COOKIE).filter(|v| !v.is_empty());`:

```rust
    if let Some(token) = cookie_value(&headers, crate::session::PORTAL_SESSION_COOKIE)
        .filter(|v| !v.is_empty())
    {
        if let Some(sub) = crate::session::verify_session(&config.session, &token)
            .map(|session| session.sub.clone())
        {
            let _ = config
                .event_bus
                .publish(&crate::events::Event::new(
                    "session.logout",
                    &sub,
                    &config.opensme_domain,
                    crate::events::Visibility::Recipients(vec![sub.clone()]),
                    serde_json::json!({}),
                ))
                .await;
        }
    }
```

The local variable `sub` is the verified OIDC subject; a missing/tampered cookie publishes nothing and leaves the 303/cookie-clear response unchanged.

- [ ] **Step 3: verify and commit**

Run: `python3 tests/run.py --static`
Expected: full suite green (35 suites / 0 failed), mutation coverage green.
Run: `cargo fmt --all -- --check`; `git add portal/src/session.rs portal/src/logout.rs && git commit -m "feat(events): publish login and verified logout session events"`

### Task 4: docs and final verification

**Files:** `docs/superpowers/plans/2026-11-25-suite-event-producers.md`, `docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`

- [ ] **Step 1: close the plan and mark the follow-up slice**

Check every `- [ ]` step in `docs/superpowers/plans/2026-11-25-suite-event-producers.md` as `- [x]` (keep the literal `- [ ]` syntax example unchanged if present). In `docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`, annotate the cloud/session producer follow-up with the new commit/artifacts.

- [ ] **Step 2: full verification**

Run: `python3 tests/run.py --static` → expect all suites PASS, 0 failed (only the intentional seaweedfs `:latest` warning).
Run: `python3 tests/00-static/check_mutations.py` → expect all mutations caught, including `cloud-mkdir-publish-removed`.
Run: `cargo fmt --all -- --check` → clean.
Run: `git status --short` → clean.
If `vhrz2392` resolves, run `ssh vhrz2392 'cd <src> && cargo test --release'` for the Rust unit tests; otherwise note that CI is the compile/test gate (AGENTS.md forbids local portal builds).

- [ ] **Step 3: commit**

```bash
git add docs/superpowers/plans/2026-11-25-suite-event-producers.md docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md
git commit -m "docs(events): close cloud/session producer plan and follow-up"
```
