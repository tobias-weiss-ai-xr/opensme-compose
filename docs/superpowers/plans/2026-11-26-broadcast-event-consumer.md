# Broadcast Event Consumer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** convert operator broadcasts from direct server-side fanout into a queued event consumed by a portal-web worker.

**Architecture:** `POST /api/broadcast` stores the note then publishes `broadcast.requested` through a new cookie-gated backend endpoint; a Node instrumentation worker in portal-web consumes the Redis stream with a consumer group and calls the existing `fanoutBroadcast` with XACK/XAUTOCLAIM semantics.

**Tech Stack:** Rust/axum + Redis XADD, Next.js route handlers + `instrumentation.ts` + ioredis, vitest, Python Layer-0 guard.

---

### Task 1: RED — pin the queued-broadcast contract

**Files:** `tests/00-static/check_broadcast_consumer.py`, `tests/run.py`, `tests/00-static/check_mutations.py`, `docs/VALIDATION.md`

- [x] **Step 1: create the check**

```python
#!/usr/bin/env python3
"""Layer 0 pins: broadcasts are queued, not fanned out inline."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

MAIN = (ROOT / "portal" / "src" / "main.rs").read_text()
ROUTE = (ROOT / "portal" / "web" / "src" / "app" / "api" / "broadcast" / "route.ts").read_text()
INSTR = (ROOT / "portal" / "web" / "instrumentation.ts").read_text() \
    if (ROOT / "portal" / "web" / "instrumentation.ts").exists() else ""
PANEL = (ROOT / "portal" / "web" / "src" / "components" / "IntercomPanel.tsx").read_text()
SPEC = (ROOT / "docs" / "superpowers" / "specs" / "2026-11-21-portal-realtime-events-design.md").read_text()


def main():
    result = Result("broadcast-consumer")
    result.header("Layer 0: queued broadcast producer/consumer")
    check = lambda cond, msg: (result.ok if cond else result.fail)(msg)

    check('.route("/api/events/publish", post(handle_event_publish))' in MAIN,
          "B1 backend exposes the cookie-gated event publish route")
    check('"broadcast.requested"' in MAIN and '"broadcast.requested"' in ROUTE,
          "B2 producer and web route agree on the event kind")
    check("events::Visibility::Team" in MAIN,
          "B3 broadcast event is visible to all authenticated viewers")
    check("fanoutBroadcast" not in ROUTE and "queued" in ROUTE,
          "B4 broadcast route queues instead of fanning out inline")
    check("xreadgroup" in INSTR and "portal-web-broadcast" in INSTR
          and "xack" in INSTR and "xautoclaim" in INSTR
          and "fanoutBroadcast" in INSTR,
          "B5 portal-web worker consumes the group and calls fanoutBroadcast")
    check("res.queued" in PANEL and "vorgemerkt" in PANEL,
          "B6 UI reports queued delivery instead of channel outcomes")
    check("event-driven consumers" in SPEC and "2026-11-26" in SPEC,
          "B7 design follow-up slice 2 is annotated as landed")
    return result.summary()


if __name__ == "__main__":
    sys.exit(main())
```

- [x] **Step 2: register check, mutation, inventory**

In `tests/run.py` after the event-producers line add:

```python
    ("Queued broadcast consumer", "tests/00-static/check_broadcast_consumer.py"),
```

In `tests/00-static/check_mutations.py` append to `MUTATIONS`:

```python
    {
        "name": "broadcast-inline-fanout-restored",
        "file": "portal/web/src/app/api/broadcast/route.ts",
        "old": "const fanout = await fanoutBroadcast(text);",
        "new": "const queuedx = await fanoutBroadcast(text);",
        "check": "tests/00-static/check_broadcast_consumer.py",
        "why": "broadcast-consumer — inline fanout must break the queue pin",
    },
```

In `docs/VALIDATION.md` add the inventory line:

```markdown
- [x] `check_broadcast_consumer.py` — queued broadcasts: publish endpoint,
      kind/visibility, no inline fanout, portal-web consumer group with
      XACK/XAUTOCLAIM, queued UI status
```

- [x] **Step 3: prove RED and commit**

Run: `python3 tests/run.py --static` → expect B1–B7 failures.
Run: `git add tests/00-static/check_broadcast_consumer.py tests/run.py tests/00-static/check_mutations.py docs/VALIDATION.md && git commit -m "test(layer0): pin queued broadcast consumer (RED)"`

### Task 2: backend publish endpoint

**Files:** `portal/src/main.rs`

- [x] **Step 1: add the route and handler**

Register immediately after `.route("/api/events", get(handle_events))`:

```rust
        .route("/api/events/publish", post(handle_event_publish))
```

Add after `handle_events`:

```rust
/// POST /api/events/publish — same-origin, session-gated producer endpoint.
/// Only allowlisted kinds may be published by the browser; the actor comes
/// from the verified session, never from the request body.
async fn handle_event_publish(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    Json(body): Json<EventPublishBody>,
) -> impl IntoResponse {
    if body.kind != "broadcast.requested" {
        return (StatusCode::BAD_REQUEST, "kind not allowed").into_response();
    }
    if body.text.trim().is_empty() || body.text.chars().count() > 2000 {
        return (StatusCode::BAD_REQUEST, "invalid text").into_response();
    }
    let _ = config
        .event_bus
        .publish(&events::Event::new(
            "broadcast.requested",
            &session.sub,
            &config.opensme_domain,
            events::Visibility::Team,
            serde_json::json!({ "text": body.text }),
        ))
        .await;
    StatusCode::ACCEPTED.into_response()
}

#[derive(Deserialize)]
struct EventPublishBody {
    kind: String,
    text: String,
}
```

- [x] **Step 2: add a unit test**

In the `#[cfg(test)]` module append:

```rust
    #[tokio::test]
    async fn broadcast_event_is_team_visible_and_carries_only_text() {
        let bus = events::EventBus::memory();
        let e = events::Event::new(
            "broadcast.requested",
            "alice",
            "example.org",
            events::Visibility::Team,
            serde_json::json!({ "text": "Hallo" }),
        );
        bus.publish(&e).await.unwrap();
        let all = bus.replay(None).await.unwrap();
        assert_eq!(all[0].payload["text"], "Hallo");
        assert!(all[0].visibility.visible_to(&events::Viewer {
            sub: "bob".into(),
            email: "".into(),
            name: "".into(),
            tenant: "example.org".into(),
        }));
    }
```

- [x] **Step 3: verify and commit**

Run: `python3 tests/run.py --static` → B1–B3 pass, B4–B7 still fail.
Run: `cargo fmt --all -- --check` with workdir `portal/`.
Run: `git add portal/src/main.rs && git commit -m "feat(events): accept queued broadcast requests from the portal"`

### Task 3: web route, API type and UI status

**Files:** `portal/web/src/app/api/broadcast/route.ts`, `portal/web/src/lib/api.ts`, `portal/web/src/components/IntercomPanel.tsx`, their test files

- [x] **Step 1: replace inline fanout with queueing**

In `portal/web/src/app/api/broadcast/route.ts`, drop `import { fanoutBroadcast } from "@/lib/broadcast";`. Replace the final two lines with:

```ts
  // Legs 2+3 are queued: the worker consumes broadcast.requested and calls
  // fanoutBroadcast; the HTTP response reports acceptance, not delivery.
  const queueBody = JSON.stringify({ kind: "broadcast.requested", text });
  try {
    const queued = await forwardToBackend({
      url: `${backendUrl()}/api/events/publish`,
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(cookie ? { cookie } : {}),
      },
      body: queueBody,
      timeoutMs: 15_000,
    });
    if (queued.status === 401) {
      return NextResponse.json({ error: "unauthorized" }, { status: 401 });
    }
    if (queued.status < 200 || queued.status >= 300) {
      return NextResponse.json(
        { error: `queue failed: HTTP ${queued.status}` },
        { status: queued.status },
      );
    }
  } catch {
    return NextResponse.json({ error: "event stream unreachable" }, { status: 502 });
  }

  return NextResponse.json({ message, queued: true });
```

- [x] **Step 2: update the API type**

In `portal/web/src/lib/api.ts`, change `postBroadcast`'s return type to:

```ts
export async function postBroadcast(text: string): Promise<{
  message: IntercomMessage;
  queued: boolean;
}> {
```

- [x] **Step 3: update the UI message**

In `portal/web/src/components/IntercomPanel.tsx` replace the destructuring/status block:

```ts
      if (!res.queued) throw new Error("Broadcast nicht in die Warteschlange aufgenommen");
      setBroadcastText("");
      setBroadcastOut("Broadcast vorgemerkt — Zustellung läuft im Hintergrund.");
```

- [x] **Step 4: update vitest tests**

In `portal/web/src/app/api/broadcast/route.test.ts`, add a mock fetch case for `/api/events/publish` returning 202 and assert the response is `{message, queued:true}`; in `portal/web/src/components/IntercomPanel.test.tsx`, change the broadcast-success test to return `{message, queued:true}` and expect the "vorgemerkt" status. Keep the existing 401/unauthorized tests.

- [x] **Step 5: verify and commit**

Run: `cd portal/web && bun run test && bun run typecheck`.
Run: `python3 tests/run.py --static` → B4/B6 pass, B5/B7 still fail.
Run: `git add portal/web/src && git commit -m "feat(portal-web): queue broadcasts instead of fanning out inline"`

### Task 4: portal-web stream worker

**Files:** `portal/web/instrumentation.ts`, `portal/web/package.json`, `portal/web/bun.lock`

- [x] **Step 1: add the ioredis dependency**

Run: `cd portal/web && bun add ioredis`. This updates `package.json` and `bun.lockb`/`bun.lock` (whatever bun uses in this repo).

- [x] **Step 2: create the instrumentation worker**

Create `portal/web/instrumentation.ts`:

```ts
/**
 * Next.js instrumentation hook: consume queued broadcasts from the portal
 * event stream and run the existing best-effort Matrix/mail fanout.
 * One consumer per web process; a Redis consumer group deduplicates across
 * replicas. Failed deliveries stay pending for XAUTOCLAIM after 5 minutes.
 */
import Redis from "ioredis";
import { fanoutBroadcast } from "./src/lib/broadcast";

const STREAM = process.env.EVENTS_STREAM || "opensme:events";
const GROUP = "portal-web-broadcast";
const CONSUMER = `${process.env.HOSTNAME || "portal-web"}-${process.pid}`;
const CLAIM_MS = 5 * 60 * 1000;

async function deliver(redis: Redis, id: string, fields: Record<string, string>) {
  if (fields.kind !== "broadcast.requested") {
    await redis.xack(STREAM, GROUP, id);
    return;
  }
  const payload = JSON.parse(fields.payload || "{}") as { text?: string };
  const text = typeof payload.text === "string" ? payload.text.trim() : "";
  if (!text) {
    await redis.xack(STREAM, GROUP, id);
    return;
  }
  try {
    await fanoutBroadcast(text);
    await redis.xack(STREAM, GROUP, id);
  } catch (err) {
    console.error("broadcast fanout failed; entry remains pending", err);
  }
}

export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const url = process.env.REDIS_URL;
  if (!url) return;
  const redis = new Redis(url, { lazyConnect: false, maxRetriesPerRequest: null });
  try {
    await redis.xgroup("CREATE", STREAM, GROUP, "0", true);
  } catch {
    // group already exists — normal on restart
  }

  const claim = async () => {
    const pending = await redis.xautoclaim(
      STREAM, GROUP, CONSUMER, CLAIM_MS, "0-0", "COUNT", 10,
    );
    for (const [id, fields] of pending[1] as Array<[string, string[]]>) {
      const obj: Record<string, string> = {};
      for (let i = 0; i < fields.length; i += 2) obj[fields[i]] = fields[i + 1];
      await deliver(redis, id, obj);
    }
  };
  await claim();

  const loop = async () => {
    while (true) {
      const reply = (await redis.xreadgroup(
        "GROUP", GROUP, CONSUMER, "COUNT", 10, "BLOCK", 5000,
        "STREAMS", STREAM, ">",
      )) as Array<[string, Array<[string, string[]]>]> | null;
      for (const [, entries] of reply || []) {
        for (const [id, fields] of entries) {
          const obj: Record<string, string> = {};
          for (let i = 0; i < fields.length; i += 2) obj[fields[i]] = fields[i + 1];
          await deliver(redis, id, obj);
        }
      }
    }
  };
  void loop();
}
```

- [x] **Step 3: verify and commit**

Run: `cd portal/web && bun run typecheck && bun run test`.
Run: `python3 tests/run.py --static` → full gate green.
Run: `git add portal/web/instrumentation.ts portal/web/package.json portal/web/bun.lock* && git commit -m "feat(portal-web): consume queued broadcasts from Redis stream"`

### Task 5: docs, plan closeout, final verification

**Files:** `docs/superpowers/plans/2026-11-26-broadcast-event-consumer.md`, `docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`

- [x] **Step 1: annotate and close**

In `docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`, annotate follow-up 2 with the landed artifacts (spec `8f2db12`, guard/endpoint/worker commits) and note that direct SOGo/OpenCloud/IdP producers remain follow-up 1. Check all real plan steps `[x]` (preserve the line-3 syntax example).

- [x] **Step 2: final verification**

Run: `python3 tests/run.py --static` → expect all suites PASS, 0 failed.
Run: `python3 tests/00-static/check_mutations.py` → expect `broadcast-inline-fanout-restored` caught.
Run: `cd portal/web && bun run test && bun run typecheck`.
Run: `cargo fmt --all -- --check` from `portal/`.
Run: `git status --short` → clean. Remote `cargo test` only if `vhrz2392` resolves; otherwise CI is the compile gate.

- [x] **Step 3: commit**

```bash
git add docs/superpowers/plans/2026-11-26-broadcast-event-consumer.md docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md
git commit -m "docs(events): close queued-broadcast plan and follow-up"
```
