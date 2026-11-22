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
    check(re.search(r'new\s+Ctor\(\s*["\']/api/events["\']\s*\)', client) is not None,
          "E3 EventSource opens the same-origin /api/events route")
    # E4 — pin each PRODUCTION task publish; main.rs has a fourth
    # Visibility::Recipients in a unit test, so a total count would let a
    # missing publish hide behind the test fixture.
    production_visibility = "events::Visibility::Recipients(recipients_for_task("
    for kind, task_expr in (
        ("task.created", "&t, &session.sub"),
        ("task.updated", "&t, &session.sub"),
        ("task.deleted", "&task, &session.sub"),
    ):
        pattern = (kind + r"[\s\S]{0,200}" + re.escape(production_visibility)
                   + re.escape(task_expr) + r"\)")
        check(re.search(pattern, main) is not None,
              f"E4 {kind} publish passes explicit recipient visibility")
    check(not re.search(r'task\.(created|updated|deleted)[\s\S]{0,200}Visibility::Team', main),
          "E4 task events never use Visibility::Team")
    # E5
    panel = WEB / "components" / "IntercomPanel.tsx"
    check("POLL_MS" in (panel.read_text() if panel.exists() else ""),
          "E5 polling fallback retained")
    check("excludedContentTypes=text/event-stream" in COMPOSE.read_text(),
          "E5 Traefik compression excludes text/event-stream")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
