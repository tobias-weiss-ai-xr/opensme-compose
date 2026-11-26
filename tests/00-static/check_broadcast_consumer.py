#!/usr/bin/env python3
"""Layer 0 pins: broadcasts are queued, not fanned out inline."""
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
    start = MAIN.find("async fn handle_event_publish(")
    block = MAIN[start: MAIN.find("\n/// ", start)] if start >= 0 else ""
    check("events::Visibility::Team" in block,
          "B3 broadcast publish handler is visible to all authenticated viewers")
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
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
