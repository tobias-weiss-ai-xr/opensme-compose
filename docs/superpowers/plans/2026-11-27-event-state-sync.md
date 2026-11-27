# Event State Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the portal SSE stream resume correctly after reconnect/reload and suppress duplicate dispatch of replayed frames.

**Architecture:** Fix the Redis cursor-live peek, accept a `?since=` cursor alongside `Last-Event-ID`, forward the query through the same-origin web proxy, and dedupe by SSE event ID in `useEventStream` with a bounded `sessionStorage` set. Existing panel reconciliation and polling fallback stay unchanged.

**Tech Stack:** Rust/axum + redis, Next.js route handlers, React `EventSource`, vitest, Layer-0 Python guards.

---

### Task 1: RED Layer-0 guard

**Files:**
- Create: `tests/00-static/check_event_sync.py`
- Modify: `tests/run.py`
- Modify: `tests/00-static/check_mutations.py`
- Modify: `docs/VALIDATION.md`
- Modify: `tests/00-static/check_events.py`

- [x] **Step 1: Create the guard with the exact contract**

```python
#!/usr/bin/env python3
"""Layer 0 pins for SSE resume + client-side dedupe (event state sync)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT  # noqa: E402

EVENTS = (ROOT / "portal" / "src" / "events.rs").read_text()
MAIN = (ROOT / "portal" / "src" / "main.rs").read_text()
PROXY = (ROOT / "portal" / "web" / "src" / "app" / "api" / "events" / "route.ts").read_text()
HOOK = (ROOT / "portal" / "web" / "src" / "lib" / "useEventStream.ts").read_text()


def main() -> int:
    result = Result("event-sync")
    result.header("Layer 0: SSE resume cursor and client dedupe")

    def check(cond: bool, msg: str) -> None:
        if cond:
            result.ok(msg)
        else:
            result.fail(msg)

    check('redis::cmd("XREVRANGE")' in EVENTS,
          "S1 newest-entry peek uses XREVRANGE (not inverted XRANGE)")
    check("sanitize_cursor(last_id)" in EVENTS and "newest < cursor" in EVENTS,
          "S2 cursor validation and stale-cursor reset stay intact")
    check("struct EventsQuery" in MAIN and "since: Option<String>" in MAIN,
          "S3 handle_events accepts an explicit ?since= cursor")
    check("query.since" in MAIN and "last-event-id" in MAIN,
          "S4 header wins, query is the reload fallback")
    check("req.nextUrl?.search" in PROXY and "last-event-id" in PROXY,
          "S5 proxy forwards both query and Last-Event-ID")
    check('"opensme:events:last-id"' in HOOK and "?since=" in HOOK,
          "S6 hook resumes from the stored SSE id")
    check("lastEventId" in HOOK and "MAX_SEEN_IDS = 512" in HOOK,
          "S7 hook dedupes by SSE id with a bounded set")
    check('dispatchEvent({ id: "sse-ready", kind: SSE_READY_KIND, payload: {} })' in HOOK,
          "S8 synthetic ready dispatch stays unconditional")
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [x] **Step 2: Relax the old hook-URL pin and update the cursor command pin in `tests/00-static/check_events.py`**

Replace the exact `new Ctor(...)` regex in E3 with a prefix check:

```python
        'const es = new Ctor(' in hook
        and '"/api/events' in hook,
```

Replace `"XRANGE"` in the E7 string list with `"XREVRANGE"`.

- [x] **Step 3: Register the guard in `tests/run.py`**

Insert after the `check_events` entry:

```python
    ("Event state sync", "tests/00-static/check_event_sync.py"),
```

- [x] **Step 4: Add the mutation**

Insert after the existing `events-sse-compression-exclusion` entry in `tests/00-static/check_mutations.py`:

```python
    {
        "name": "sse-resume-cursor-broken",
        "file": "portal/src/events.rs",
        "old": 'redis::cmd("XREVRANGE")',
        "new": 'redis::cmd("XRANGE")',
        "check": "tests/00-static/check_event_sync.py",
        "why": "event-sync — inverting the newest-entry peek silently drops resume cursors",
    },
```

- [x] **Step 5: Add the `docs/VALIDATION.md` inventory line**

After the `check_events.py` inventory block:

```markdown
- `tests/00-static/check_event_sync.py`: XREVRANGE newest-entry peek, `?since=`
  fallback + header precedence, proxy query forwarding, bounded SSE-id dedupe,
  unconditional ready dispatch.
```

- [x] **Step 6: Verify RED**

Run: `python3 tests/00-static/check_event_sync.py`
Expected: at least S1, S3, S5, S6, S7 fail (exit 1).

- [x] **Step 7: Commit**

```bash
git add tests/00-static/check_event_sync.py tests/run.py tests/00-static/check_mutations.py docs/VALIDATION.md tests/00-static/check_events.py
git commit -m "test(layer0): pin SSE resume and dedupe contract (RED)"
```

---

### Task 2: Backend resume fixes

**Files:**
- Modify: `portal/src/events.rs:482-489`, `:241-247`, `:623+`
- Modify: `portal/src/main.rs:1224-1247`

- [x] **Step 1: Fix the cursor-live peek**

In `ensure_cursor_live()`, replace:

```rust
        let reply: redis::RedisResult<redis::Value> = redis::cmd("XRANGE")
            .arg(&self.config.stream)
            .arg("+")
            .arg("-")
            .arg("COUNT")
            .arg(1)
            .query_async(conn)
            .await;
```

with:

```rust
        let reply: redis::RedisResult<redis::Value> = redis::cmd("XREVRANGE")
            .arg(&self.config.stream)
            .arg("+")
            .arg("-")
            .arg("COUNT")
            .arg(1)
            .query_async(conn)
            .await;
```

Keep the comment and reply parsing unchanged; `XREVRANGE` returns the same
`[[id, fields], ...]` shape with newest first.

- [x] **Step 2: Honor `since` in the in-memory replay**

Replace `replay`:

```rust
    /// Used by tests and by the SSE reader for backfill on reconnect.
    pub async fn replay(&self, since: Option<&str>) -> Result<Vec<Event>, String> {
        match &self.0 {
            BusInner::Disabled => Ok(Vec::new()),
            BusInner::Memory { events, .. } => {
                let all = events.lock().await.clone();
                // The in-memory backend assigns `m-<n>` IDs; Redis IDs are
                // handled by EventReader, not by replay.
                let Some(since) = since
                    .and_then(|id| id.strip_prefix("m-"))
                    .and_then(|n| n.parse::<u128>().ok())
                else {
                    return Ok(all);
                };
                Ok(all
                    .into_iter()
                    .filter(|event| {
                        event
                            .id
                            .strip_prefix("m-")
                            .and_then(|n| n.parse::<u128>().ok())
                            .is_some_and(|n| n > since)
                    })
                    .collect())
            }
            BusInner::Redis { .. } => Ok(Vec::new()), // live reads use EventReader
        }
    }
```

- [x] **Step 3: Pass the in-memory `since` to replay in `stream()`**

In the `BusInner::Memory` arm of `stream()`, replace the replay call with:

```rust
                let events = self.replay(last_id.as_deref()).await.unwrap_or_default();
```

- [x] **Step 4: Add the query cursor to `handle_events`**

In `portal/src/main.rs`, insert this struct immediately above `handle_events`:

```rust
#[derive(serde::Deserialize)]
struct EventsQuery {
    since: Option<String>,
}
```

Change the signature and cursor selection to:

```rust
async fn handle_events(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    headers: HeaderMap,
    axum::extract::Query(query): axum::extract::Query<EventsQuery>,
) -> impl IntoResponse {
    if config.event_bus.is_disabled() {
        return StatusCode::NO_CONTENT.into_response();
    }
    let last = headers
        .get("last-event-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
        .or(query.since);
```

Keep the viewer construction and `config.event_bus.stream(viewer, last).await`
unchanged.

- [x] **Step 5: Add a Rust unit test**

Append to `mod tests` in `portal/src/events.rs`:

```rust
    #[tokio::test]
    async fn replay_since_filters_in_memory_events_by_stream_id() {
        let bus = EventBus::memory();
        let first = Event::new(
            "task.created", "alice", "t", Visibility::Team,
            serde_json::json!({ "id": 1 }),
        );
        let second = Event::new(
            "task.created", "alice", "t", Visibility::Team,
            serde_json::json!({ "id": 2 }),
        );
        let first_id = bus.publish(&first).await.unwrap();
        let second_id = bus.publish(&second).await.unwrap();

        let after = bus.replay(Some(&first_id)).await.unwrap();
        assert_eq!(after.len(), 1, "cursor must be exclusive");
        assert_eq!(after[0].id, second_id);

        let invalid = bus.replay(Some("not-an-id")).await.unwrap();
        assert_eq!(invalid.len(), 2, "invalid cursor falls back to full replay");
    }
```

- [x] **Step 6: Verify**

Run: `cd portal && cargo fmt --all && cargo fmt --all -- --check`
Expected: clean formatting, no local build (AGENTS.md).

Run: `python3 tests/00-static/check_event_sync.py`
Expected: S1–S4 pass; S5–S8 still fail.

- [x] **Step 7: Commit**

```bash
git add portal/src/events.rs portal/src/main.rs
git commit -m "fix(events): resume SSE stream from query or Last-Event-ID"
```

---

### Task 3: Proxy and client dedupe

**Files:**
- Modify: `portal/web/src/app/api/events/route.ts`
- Modify: `portal/web/src/app/api/events/route.test.ts`
- Modify: `portal/web/src/lib/useEventStream.ts`
- Modify: `portal/web/src/lib/useEventStream.test.ts`

- [x] **Step 1: Forward the query in the SSE proxy**

In `portal/web/src/app/api/events/route.ts`, add the search fallback before the
fetch and use it in the fetch URL:

```ts
  const search = req.nextUrl?.search ?? new URL(req.url).search;
  let upstream: Response;
  try {
    upstream = await fetch(`${backendUrl()}/api/events${search}`, {
```

The optional chaining keeps plain `Request` objects in unit tests working;
`NextRequest` supplies `nextUrl` in production. Keep all headers and response
streaming unchanged.

- [x] **Step 2: Update the proxy test**

In `portal/web/src/app/api/events/route.test.ts`, add a second test after the existing one:

```ts
  it("forwards a since cursor to the backend", async () => {
    const upstream = new ReadableStream<Uint8Array>({
      start(controller) { controller.close(); },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: upstream,
      headers: new Headers({ "content-type": "text/event-stream" }),
    }));
    const req = new Request("http://localhost/api/events?since=42-1", {
      headers: { cookie: "OPENSMEPORTALSESSION=abc" },
    });
    await GET(req as never);
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
      "http://backend.test/api/events?since=42-1",
    );
  });
```

- [x] **Step 3: Implement resume + bounded dedupe in the hook**

Replace `portal/web/src/lib/useEventStream.ts` with:

```ts
"use client";

import { useEffect, useRef, useState } from "react";
import { dispatchEvent, SSE_READY_KIND, type PortalEvent } from "./events";

/** Test seam: override the global EventSource constructor in unit tests. */
let EventSourceCtor: typeof EventSource | null = null;
export function resetEventSourceForTests() {
  EventSourceCtor = null;
}

const LAST_ID_KEY = "opensme:events:last-id";
const SEEN_KEY = "opensme:events:seen";
const MAX_SEEN_IDS = 512;

const LISTENED_KINDS = [
  "intercom.created", "intercom.deleted",
  "task.created", "task.updated", "task.deleted",
  "poll.created", "poll.voted",
];

function readSeen(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.sessionStorage.getItem(SEEN_KEY);
    const ids = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : []);
  } catch {
    return new Set();
  }
}

function remember(id: string, seen: Set<string>): void {
  seen.add(id);
  while (seen.size > MAX_SEEN_IDS) {
    const oldest = seen.values().next().value;
    if (typeof oldest !== "string") break;
    seen.delete(oldest);
  }
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(SEEN_KEY, JSON.stringify([...seen]));
    window.sessionStorage.setItem(LAST_ID_KEY, id);
  } catch { /* storage may be disabled; in-memory dedupe still works */ }
}

/**
 * Open the same-origin SSE stream, resuming from the last seen stream ID after
 * reload. Native EventSource reconnect supplies Last-Event-ID automatically;
 * the `?since=` query covers fresh mounts in the same tab.
 */
export function useEventStream(authenticated: boolean): { connected: boolean } {
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!authenticated || typeof window === "undefined") return;
    const Ctor = EventSourceCtor ?? window.EventSource;
    if (!Ctor) return;
    let since = "";
    try { since = window.sessionStorage.getItem(LAST_ID_KEY) ?? ""; } catch { /* disabled */ }
    const seen = readSeen();
    const es = new Ctor(since ? `/api/events?since=${encodeURIComponent(since)}` : "/api/events");
    sourceRef.current = es;
    es.onerror = () => setConnected(false);
    es.addEventListener("ready", () => {
      setConnected(true);
      dispatchEvent({ id: "sse-ready", kind: SSE_READY_KIND, payload: {} });
    });
    for (const kind of LISTENED_KINDS) {
      es.addEventListener(kind, (raw) => {
        try {
          const msg = raw as MessageEvent;
          const event = JSON.parse(msg.data) as PortalEvent;
          event.kind = kind;
          const id = msg.lastEventId || event.id;
          if (id && seen.has(id)) return;
          if (id) remember(id, seen);
          dispatchEvent(event);
        } catch { /* ignore malformed frames */ }
      });
    }
    return () => { es.close(); sourceRef.current = null; setConnected(false); };
  }, [authenticated]);

  return { connected };
}
```

- [x] **Step 4: Update hook tests**

In `portal/web/src/lib/useEventStream.test.ts`, extend `FakeEventSource.emit` to
carry `lastEventId`, clear `sessionStorage` in `beforeEach`, and add tests:

```ts
  emit(type: string, data: unknown, lastEventId = "") {
    this.listeners[type]?.(new MessageEvent(type, {
      data: JSON.stringify(data),
      lastEventId,
    }));
  }
```

```ts
  it("resumes from the stored SSE id after a remount", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    window.sessionStorage.setItem("opensme:events:last-id", "42-1");
    const { unmount } = renderHook(() => useEventStream(true));
    expect(FakeEventSource.last?.url).toBe("/api/events?since=42-1");
    unmount();
  });

  it("drops replayed frames with the same SSE id", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const received: unknown[] = [];
    subscribeEvents((e) => received.push(e));
    renderHook(() => useEventStream(true));
    const frame = { id: "7-0", kind: "poll.voted", payload: { poll: { id: 7 } } };
    act(() => FakeEventSource.last?.emit("poll.voted", frame, "7-0"));
    act(() => FakeEventSource.last?.emit("poll.voted", frame, "7-0"));
    expect(received).toHaveLength(1);
  });
```

Change the existing URL assertion from `"/api/events"` to `"/api/events"`
only when storage is empty (the new `beforeEach` clears it, so no change is
needed to that line).

- [x] **Step 5: Verify**

Run: `cd portal/web && bun run test && bun run typecheck`
Expected: all tests pass, typecheck clean.

Run: `python3 tests/00-static/check_event_sync.py`
Expected: 8 passed / 0 failed.

- [x] **Step 6: Commit**

```bash
git add portal/web/src/app/api/events/route.ts portal/web/src/app/api/events/route.test.ts portal/web/src/lib/useEventStream.ts portal/web/src/lib/useEventStream.test.ts
git commit -m "feat(portal-web): resume SSE stream and dedupe replayed frames"
```

---

### Task 4: Closeout and full verification

**Files:**
- Modify: `docs/superpowers/plans/2026-11-27-event-state-sync.md`
- Modify: `docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`

- [x] **Step 1: Close all real plan step boxes** (`- [ ]` → `- [x]`), leaving the line-3 syntax example unchanged.

- [x] **Step 2: Annotate realtime-events design follow-up item 3**

Append to the polls-events annotation in
`docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`:

```markdown
   Richer client state sync — DONE 2026-11-27: the SSE reader peeks the newest
   Redis ID with XREVRANGE, `handle_events` accepts `?since=` in addition to
   Last-Event-ID, the web proxy forwards the query, and `useEventStream`
   deduplicates replayed frames by SSE ID with a bounded sessionStorage set.
   Pinned by `tests/00-static/check_event_sync.py` (design `16a9ec9`).
```

- [x] **Step 3: Full verification**

```bash
python3 tests/run.py --static
python3 tests/00-static/check_mutations.py
(cd portal/web && bun run test && bun run typecheck)
(cd portal && cargo fmt --all -- --check)
```

Expected: static 40 passed / 0 failed; mutation suite 77 passed / 0 failed with
35/35 wired checks; vitest all green; typecheck and fmt clean.

- [x] **Step 4: Commit and push**

```bash
git add docs/superpowers/plans/2026-11-27-event-state-sync.md docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md
git commit -m "docs(events): close SSE state sync plan and follow-up"
git push
```
