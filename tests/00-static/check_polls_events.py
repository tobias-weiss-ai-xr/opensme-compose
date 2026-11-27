#!/usr/bin/env python3
"""Layer 0 pins for polls realtime events (follow-up 3)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

MAIN = (ROOT / "portal" / "src" / "main.rs").read_text()
HOOK = (ROOT / "portal" / "web" / "src" / "lib" / "useEventStream.ts").read_text()
PANEL = (ROOT / "portal" / "web" / "src" / "components" / "PollsPanel.tsx").read_text()


def handler(fn: str) -> str:
    start = MAIN.index(fn)
    end = MAIN.find("\n/// ", start)
    return MAIN[start:end if end > 0 else len(MAIN)]


def main():
    result = Result("polls-events")
    result.header("Layer 0: polls event producers and consumers")

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)

    for fn, kind in (
        ("async fn handle_polls_create(", "poll.created"),
        ("async fn handle_polls_upload(", "poll.created"),
        ("async fn handle_polls_vote(", "poll.voted"),
    ):
        block = handler(fn)
        check(f'"{kind}"' in block and ".event_bus" in block
              and "events::Event::new(" in block,
              f"P1 {fn} publishes {kind}")
        check("events::Visibility::Team" in block,
              f"P2 {fn} uses Team visibility")
        check("poll.public(None)" in block and "voted_by" not in block,
              f"P3 {fn} payload is viewer-neutral (no voted_by)")

    check('"poll.created"' in HOOK and '"poll.voted"' in HOOK,
          "P4 event hook listens to poll.created and poll.voted")
    check("subscribeEvents" in PANEL and "poll.created" in PANEL
          and "poll.voted" in PANEL and "portal.ready" in PANEL,
          "P5 PollsPanel subscribes, merges and reconciles on ready")
    check("refresh" in PANEL and "dedup" not in PANEL.lower(),
          "P6 panel keeps a refresh path (no duplicate-id workaround)")
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
