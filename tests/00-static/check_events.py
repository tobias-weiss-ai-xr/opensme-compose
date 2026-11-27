#!/usr/bin/env python3
"""Layer 0 pins for portal real-time events (slice 1).

E1 — events.rs defines Event/Visibility/EventBus and uses XADD (Redis Streams).
E2 — /api/events is on the protected router, before require_session is layered.
E3 — the frontend proxy forwards the cookie and returns a ReadableStream;
     no cross-origin EventSource is allowed (CSP connect-src 'self').
E4 — every publish call passes an explicit Visibility; task kinds never use
     Visibility::Team.
E5 — polling fallback still exists and Traefik excludes text/event-stream
     from compression.
E6 — the XREAD reader loop has no sleep between cycles (a 15 s sleep would
     delay every published event by up to 15 s; XREAD's 1 s block throttles
     the loop instead).
E7 — fail-soft wiring: read errors terminate the stream (poller resumes), a
     ready frame gates the poller pause, and cursor/cap/tenant guards exist.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

PORTAL = ROOT / "portal" / "src"
WEB = ROOT / "portal" / "web" / "src"
EVENTS = PORTAL / "events.rs"
MAIN = PORTAL / "main.rs"
COMPOSE = ROOT / "docker-compose.yml"


def main():
    result = Result("portal-events")
    result.header("Layer 0: portal real-time events validation")
    events = EVENTS.read_text() if EVENTS.exists() else ""
    main = MAIN.read_text() if MAIN.exists() else ""
    route = WEB / "app" / "api" / "events" / "route.ts"
    proxy = route.read_text() if route.exists() else ""

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)

    # E1
    check(all(t in events for t in [
        "pub enum Visibility", "pub struct Event", "pub struct EventBus",
        'XADD', 'XREAD',
    ]), "E1 events.rs: types + Redis Streams commands")
    # E2
    check('.route("/api/events", get(handle_events))' in main,
          "E2 /api/events registered on the protected router")
    pos_route = main.find('.route("/api/events", get(handle_events))')
    pos_layer = main.find("session::require_session")
    check(pos_route >= 0 and pos_layer >= 0 and pos_route < pos_layer,
          "E2 /api/events is inside the session firewall")
    # E3
    check(all(t in proxy for t in [
        'new Response(upstream.body', 'cookie', 'last-event-id',
        '"content-type": "text/event-stream"',
    ]), "E3 SSE proxy forwards session cookie and streams the body")
    client = WEB / "lib" / "useEventStream.ts"
    client = client.read_text() if client.exists() else ""
    check(not re.search(r'new\s+(?:EventSource|Ctor)\(\s*["\']https?://', client),
          "E3 no cross-origin EventSource (CSP connect-src 'self')")
    check('const es = new Ctor(' in client and '"/api/events' in client,
          "E3 EventSource opens the same-origin /api/events route")
    # E4 — pin each PRODUCTION task publish; main.rs has a fourth
    # Visibility::Recipients in a unit test, so a total count would let a
    # missing publish hide behind the test fixture.
    for kind, helper, task_expr in (
        ("task.created", "recipients_for_task", r"&t,\s*&session\.sub"),
        ("task.updated", "recipients_for_task_update", r"&task,\s*&t,\s*&session\.sub"),
        ("task.deleted", "recipients_for_task", r"&task,\s*&session\.sub"),
    ):
        production_visibility = "events::Visibility::Recipients(" + helper + "("
        pattern = (kind + r"[\s\S]{0,200}" + re.escape(production_visibility)
                   + r"\s*" + task_expr + r"\s*,?\s*\)")
        check(re.search(pattern, main) is not None,
              f"E4 {kind} publish passes explicit recipient visibility")
    check(not re.search(r'task\.(created|updated|deleted)[\s\S]{0,200}Visibility::Team', main),
          "E4 task events never use Visibility::Team")
    # E5 — pin the poller structurally, not just the POLL_MS identifier.
    for panel_name in ("IntercomPanel.tsx", "TasksPanel.tsx"):
        panel = (WEB / "components" / panel_name)
        text = panel.read_text() if panel.exists() else ""
        check(all(t in text for t in [
            "setInterval", "POLL_MS", "pollDelta", "data-events-connected", "fetch",
        ]), f"E5 {panel_name}: delta poller + SSE guard retained")
        check('if (event.kind === SSE_READY_KIND) void fullLoad();' in text,
              f"E7 {panel_name}: reconciles from the store when SSE becomes live")
    check("excludedContentTypes=text/event-stream" in COMPOSE.read_text(),
          "E5 Traefik compression excludes text/event-stream")
    # E6
    check("tokio::time::sleep" not in events,
          "E6 no sleep stall between XREAD cycles (events delivered within ~1s)")
    # E7 — fail-soft and delivery-wiring guards that protect the polling
    # fallback from silently becoming the only live data path.
    check(all(t in events for t in [
        "errors >= 3", "std::io::Error::other", "event: ready", "sanitize_cursor(last_id)",
        "e.tenant == viewer.tenant", "StreamSlot", "get_multiplexed_async_connection",
        "reader.ensure_cursor_live().await", "stream_id_key", "XREVRANGE", "newest < cursor",
        "if !ready && cursor_checked",
    ]), "E7 reader: error propagation, ready frame, cursor/cap/tenant guards")
    hook = (WEB / "lib" / "useEventStream.ts")
    hook = hook.read_text() if hook.exists() else ""
    check('addEventListener("ready"' in hook
          and 'onopen = () => setConnected(true)' not in hook,
          "E7 client pauses polling only after the ready frame")
    check("SSE_READY_KIND" in hook and '"sse-ready"' in hook,
          "E7 ready frame emits the local reconciliation signal")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
