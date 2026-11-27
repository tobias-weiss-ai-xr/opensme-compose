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
    check("req.nextUrl.search" in PROXY and "last-event-id" in PROXY,
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
