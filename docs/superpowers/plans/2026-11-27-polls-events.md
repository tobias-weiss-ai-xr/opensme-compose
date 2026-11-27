# Polls Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** publish viewer-neutral `poll.created`/`poll.voted` events from the polls handlers and apply them live in the portal-web polls panel.

**Architecture:** three Rust success paths publish to the existing Redis stream with `Visibility::Team` and `poll.public(None)` payloads; the frontend adds the kinds to its SSE listener and merges them in `PollsPanel`; a Layer-0 guard pins both halves.

**Tech Stack:** Rust/axum, Next.js/React with existing `useEventStream`/`subscribeEvents`, Python Layer-0 checks, Redis stream via existing `EventBus`.

---

### Task 1: RED — Layer-0 polls-event guard

**Files:** Create `tests/00-static/check_polls_events.py`; modify `tests/run.py`, `tests/00-static/check_mutations.py`, `docs/VALIDATION.md`.

- [x] **Step 1: create the check**

```python
#!/usr/bin/env python3
"""Layer 0 pins for polls realtime events."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

MAIN = (ROOT / "portal" / "src" / "main.rs").read_text()
WEB = ROOT / "portal" / "web" / "src"
HOOK = (WEB / "lib" / "useEventStream.ts").read_text()
PANEL = (WEB / "components" / "PollsPanel.tsx").read_text()


def handler(src: str, name: str) -> str:
    m = re.search(rf"async fn {name}\b", src)
    assert m, name
    tail = src[m.start():]
    nxt = re.search(r"\nasync fn ", tail[1:])
    return tail[: nxt.start() + 1] if nxt else tail


def main():
    result = Result("polls-events")
    result.header("Layer 0: polls event producers and subscriber")

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)

    for fn, kind in (
        ("handle_polls_create", "poll.created"),
        ("handle_polls_upload", "poll.created"),
        ("handle_polls_vote", "poll.voted"),
    ):
        block = handler(MAIN, fn)
        check(f'"{kind}",' in block
              and ".event_bus" in block
              and "events::Visibility::Team" in block,
              f"E-POLL-{fn}: publishes {kind} with Team visibility")
        check("poll.public(None)" in block and "voted_by" not in block,
              f"E-POLL-{fn}: payload is viewer-neutral PollPublic")

    check('"poll.created"' in HOOK and '"poll.voted"' in HOOK,
          "E-POLL-HOOK: poll kinds are listened to")
    check("subscribeEvents" in PANEL
          and '"portal.ready"' in PANEL
          and "refresh" in PANEL
          and "poll.created" in PANEL
          and "poll.voted" in PANEL,
          "E-POLL-PANEL: subscribes to polls events and reconciles on ready")
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [x] **Step 2: register in runner, mutation table, inventory**

In `tests/run.py`, after the `("Portal event producers (cloud + session)", "tests/00-static/check_event_producers.py"),` line add:

```python
    ("Polls realtime events", "tests/00-static/check_polls_events.py"),
```

In `tests/00-static/check_mutations.py`, add to `MUTATIONS`:

```python
    {
        "name": "polls-vote-publish-removed",
        "file": "portal/src/main.rs",
        "old": '                    "poll.voted",',
        "new": '                    "poll.voted-removed",',
        "check": "tests/00-static/check_polls_events.py",
        "why": "polls-events — removing the vote publish must fail the guard",
    },
```

In `docs/VALIDATION.md`, after the `check_event_producers.py` inventory line add:

```markdown
- [x] `check_polls_events.py` — polls create/upload/vote publish viewer-neutral
      Team-visible events; SSE hook + panel subscription and ready reconciliation
```

- [x] **Step 3: prove RED and commit**

Run: `python3 tests/00-static/check_polls_events.py` → expect failures for all three publish sites, hook and panel.
Run: `git add tests/00-static/check_polls_events.py tests/run.py tests/00-static/check_mutations.py docs/VALIDATION.md && git commit -m "test(layer0): pin polls realtime events (RED)"`

### Task 2: Rust poll producers

**Files:** `portal/src/main.rs`

- [x] **Step 1: publish poll.created from both creation paths**

In `handle_polls_create`, replace the final response line `Ok(Json(poll.public(None))) => ...` style success arm with:

```rust
        Ok(poll) => {
            let _ = config
                .event_bus
                .publish(&events::Event::new(
                    "poll.created",
                    &session.sub,
                    &config.opensme_domain,
                    events::Visibility::Team,
                    serde_json::json!({ "poll": poll.public(None) }),
                ))
                .await;
            (StatusCode::CREATED, Json(poll.public(None))).into_response()
        }
```

In `handle_polls_upload`, after `let poll = state.store.create(...)?;` insert the same publish block before building the response.

- [x] **Step 2: publish poll.voted after a successful vote**

In `handle_polls_vote`, on the `Ok(poll)` branch insert:

```rust
            let _ = config
                .event_bus
                .publish(&events::Event::new(
                    "poll.voted",
                    &session.sub,
                    &config.opensme_domain,
                    events::Visibility::Team,
                    serde_json::json!({ "poll": poll.public(None) }),
                ))
                .await;
```

- [x] **Step 3: add a Rust unit test**

Append to the `#[cfg(test)]` module:

```rust
    #[tokio::test]
    async fn poll_event_payload_never_exposes_voters() {
        let bus = events::EventBus::memory();
        let mut poll = polls::Poll {
            id: 1,
            question: "Open?".into(),
            options: vec!["yes".into(), "no".into()],
            votes: vec![1, 0],
            voted_by: vec!["alice".into()],
            closed: false,
            created_by: "alice".into(),
            created_at: 0,
            attachment: None,
        };
        let e = events::Event::new(
            "poll.voted",
            "alice",
            "example.org",
            events::Visibility::Team,
            serde_json::json!({ "poll": poll.public(None) }),
        );
        bus.publish(&e).await.unwrap();
        let all = bus.replay(None).await.unwrap();
        assert_eq!(all[0].payload["poll"]["votes"][0], 1);
        assert!(all[0].payload.to_string().find("voted_by").is_none());
    }
```

(Adjust the struct literal fields to the exact `polls::Poll` definition if it differs.)

- [x] **Step 4: verify and commit**

Run: `python3 tests/00-static/check_polls_events.py` → producer checks pass, hook/panel still fail.
Run: `cargo fmt --all -- --check` (workdir `portal/`; no local build per AGENTS.md).
Run: `git add portal/src/main.rs && git commit -m "feat(events): publish poll created and voted events"`

### Task 3: frontend subscription

**Files:** `portal/web/src/lib/useEventStream.ts`, `portal/web/src/components/PollsPanel.tsx`, `portal/web/src/lib/useEventStream.test.ts`, `portal/web/src/components/PollsPanel.test.tsx`

- [x] **Step 1: add kinds to the hook**

In `useEventStream.ts`, extend `LISTENED_KINDS` with:

```typescript
  "poll.created",
  "poll.voted",
```

- [x] **Step 2: subscribe in PollsPanel**

Add imports:

```typescript
import { subscribeEvents } from "@/lib/events";
import type { Poll } from "@/lib/types";
```

Inside `PollsPanel`, after `refresh` is defined:

```typescript
  useEffect(() => {
    const offs = [
      subscribeEvents("poll.created", (payload) => {
        const incoming = (payload as { poll?: Poll }).poll;
        if (!incoming) return;
        setPolls((prev) =>
          prev.some((p) => p.id === incoming.id) ? prev : [incoming, ...prev],
        );
      }),
      subscribeEvents("poll.voted", (payload) => {
        const incoming = (payload as { poll?: Poll }).poll;
        if (!incoming) return;
        setPolls((prev) =>
          prev.map((p) =>
            p.id === incoming.id
              ? { ...incoming, voted: p.voted ?? incoming.voted }
              : p,
          ),
        );
      }),
      subscribeEvents("portal.ready", () => {
        void refresh();
      }),
    ];
    return () => offs.forEach((off) => off());
  }, [refresh]);
```

- [x] **Step 3: add vitest coverage**

In `PollsPanel.test.tsx`, add a test that dispatches `poll.voted` with an updated poll and asserts the new vote count appears without a new fetch; add a test that `portal.ready` triggers `refresh`. In `useEventStream.test.ts`, assert the hook forwards `poll.created` and `poll.voted` payloads to listeners.

- [x] **Step 4: verify and commit**

Run: `python3 tests/00-static/check_polls_events.py` → all pins pass.
Run: `cd portal/web && bun run test` → all suites pass; `bun run typecheck` clean.
Run: `git add portal/web/src && git commit -m "feat(portal-web): apply polls events live in the polls panel"`

### Task 4: docs closeout and full verification

**Files:** this plan, `docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`

- [x] **Step 1: close plan and annotate follow-up**

Mark all real `- [ ]` steps in this plan `- [x]` (keep the line-3 syntax example). In the realtime-events design, annotate follow-up 3's polls item as done on 2026-11-27 with the spec/plan/commit ids; richer sync and standalone gateway remain optional.

- [x] **Step 2: full gate**

Run: `python3 tests/run.py --static` → all suites PASS, 0 failed.
Run: `python3 tests/00-static/check_mutations.py` → all mutations caught, including `polls-vote-publish-removed`.
Run: `cd portal/web && bun run test && bun run typecheck` → green.
Run: `cd portal && cargo fmt --all -- --check` → clean. Remote `cargo test` only if `vhrz2392` resolves (AGENTS.md).
Run: `git status --short` → clean.

- [x] **Step 3: commit and push**

```bash
git add docs/superpowers/plans/2026-11-27-polls-events.md docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md
git commit -m "docs(events): close polls events plan and follow-up"
git push
```
