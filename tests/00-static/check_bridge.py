#!/usr/bin/env python3
"""
tests/00-static/check_bridge.py — Layer 0 validation of the portal-web
component bridge (portal-intercom capability).

The portal renders its components (notes, feeds, cloud picker, polls) as
isolated modules. Cross-component integration goes through the typed
provider registry in portal/web/src/lib/bridge.ts — never through peer
imports. This check pins the bridge contract statically:

  R1 — Contract: lib/bridge.ts exists and declares the BridgeProviderMap
       with exactly the documented slots ("share.open", "cloud.pick").
  R2 — Every defined slot is consumed outside the registry: a slot that no
       component uses is dead integration surface ("nice-to-have" drift).
  R3 — Share wiring: the shell registers the share.open provider and
       switches the visitor into the notes tab on open.
  R4 — Notes composer consumes the prefill and posts through the note API:
       the composer must keep its URL-input fallback and the share target
       must be the existing intercom post function (no new endpoint).
  R5 — Feed source: feed cards render the "ins Team teilen" affordance
       only through the registered slot.

Pure file inspection — no docker, runs on any fresh checkout.

Usage: python3 tests/00-static/check_bridge.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

WEB = ROOT / "portal" / "web" / "src"
BRIDGE = WEB / "lib" / "bridge.ts"
SHELL = WEB / "components" / "PortalShell.tsx"
INTERCOM = WEB / "components" / "IntercomPanel.tsx"
FEED = WEB / "components" / "FeedColumn.tsx"
PICKER = WEB / "components" / "CloudFilePicker.tsx"

SLOTS = ["share.open", "cloud.pick"]


def src_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text()


def main():
    result = Result("portal-bridge")
    result.header("Layer 0: portal-web component bridge validation")
    failures = []

    bridge = src_text(BRIDGE)

    # ── R1: registry contract ────────────────────────────────────────
    if "BridgeProviderMap" not in bridge:
        failures.append(
            "R1: lib/bridge.ts must declare the BridgeProviderMap type — the "
            "documented registry contract for cross-component slots"
        )
    for slot in SLOTS:
        # Slot key as a quoted map key (exact spelling, word-bounded).
        if not re.search(rf'"{re.escape(slot)}"', bridge):
            failures.append(
                f"R1: bridge slot {slot!r} is missing from BridgeProviderMap"
            )

    # ── R2: every slot consumed outside the registry ─────────────────
    for slot in SLOTS:
        consumers = 0
        for path in sorted(WEB.rglob("*.ts*")):
            if path == BRIDGE or ".test." in path.name or ".spec." in path.name:
                continue
            if re.search(rf'"{re.escape(slot)}"', path.read_text()):
                consumers += 1
        if consumers == 0:
            failures.append(
                f"R2: slot {slot!r} is defined but consumed nowhere outside "
                "the registry — dead integration surface"
            )

    # ── R3: share wiring in the shell ────────────────────────────────
    shell = src_text(SHELL)
    if "registerProvider" not in shell or '"share.open"' not in shell:
        failures.append(
            "R3: PortalShell must register the share.open provider (it owns "
            "the tab state and forwards the prefill to the notes composer)"
        )
    elif not re.search(r"setTab\(\s*['\"]notizen['\"]", shell):
        failures.append(
            "R3: the share.open handler must switch the shell into the notes "
            "tab so the prefilled composer is visible"
        )
    elif "sharePrefill" not in shell and "openSharePayload" not in shell:
        failures.append(
            "R3: PortalShell must forward the share prefill to IntercomPanel "
            "(no peer import — shell props only)"
        )

    # ── R4: notes composer as the single share target ────────────────
    intercom = src_text(INTERCOM)
    if "postIntercom" not in intercom:
        failures.append(
            "R4: the share target must post through the existing note API — "
            "import postIntercom, never a new endpoint"
        )
    if 'type="url"' not in intercom and "ic-url" not in intercom:
        failures.append(
            "R4: the notes composer keeps its URL-input fallback for "
            "attachments (cloud absent must not break posting)"
        )
    if "cloud.pick" not in intercom:
        failures.append(
            "R4: the notes composer must consume the cloud.pick slot for its "
            "cloud attachment (shared picker, not an embedded private one)"
        )

    # ── R5: feed share source ────────────────────────────────────────
    feed = src_text(FEED)
    if '"share.open"' not in feed and "getProvider" not in feed:
        failures.append(
            "R5: FeedColumn must offer 'ins Team teilen' through the "
            "share.open provider (no affordance when no host registered)"
        )

    if failures:
        for f in failures:
            result.fail(f)
    else:
        result.ok(
            "bridge contract: slots defined + consumed, shell wires "
            "share.open → notes tab, composer posts via existing note API, "
            "feed offers the share action"
        )

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
