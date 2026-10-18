#!/usr/bin/env python3
"""
tests/00-static/check_agentic.py — AS6 agent self-maintenance gate (opt-in).

Governs how an agent may change this repository (docs/AGENTIC-AI.md §7.6,
openspec capability `agent-self-maintenance`): a change is a commit only when
the test pyramid is green, and push/deploy stays an operator decision.

This gate is a NON-DEFAULT (opt-in) capability on purpose:

  - AGENTIC_STRICT unset / "0" / "false"  → informational, NEVER fails
    (reports unregistered journeys + phantom CI references as info, exit 0).
  - AGENTIC_STRICT=1                        → enforcing: every e2e journey
    in tests/05-e2e must be a CI step (ci.yml ∪ mailcow.yml) unless it is on
    the explicit allowlist below; a phantom CI reference (step pointing at a
    missing script) fails; exit 1 on any violation.

It is intentionally NOT wired into tests/00-static/run.sh, so the regular
layer-0 suite is unaffected. Run it directly or via `make agentic-gate`;
enable the hard gate with `AGENTIC_STRICT=1`.

Exit codes: 0 = pass (always, unless AGENTIC_STRICT=1 and a violation occurs).
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
E2E = ROOT / "tests" / "05-e2e"
CI = ROOT / ".github" / "workflows" / "ci.yml"
MAILCOW = ROOT / ".github" / "workflows" / "mailcow.yml"

# Explicit allowlist: scripts in tests/05-e2e that may exist without a CI step.
#   - support modules: imported helpers / mock servers, exercised by journeys
#     that DO run (ai_mock, cloud_mock, cloud_mock_support, run).
#   - documented non-CI journeys: webmail is intentionally run against a local
#     mail stack, not the demo CI stack (which ships no SOGo/Stalwart); the saga
#     blueprints run via `make saga` because every one of them needs an overlay
#     (mail, Operaton, invoice, paperless, notes, ticketing, CMS) that the demo
#     stack does not boot — in CI they would only ever skip.
#     Remove an entry to make the strict gate enforce it in CI.
ALLOWLIST = {
    "ai_mock.py": "support module (mock LLM backend, used by ai_journey)",
    "cloud_mock.py": "support module (mock opencloud, used by journeys)",
    "cloud_mock_support.py": "support module (helpers, no journey entrypoint)",
    "run.py": "test-runner helper, not a journey",
    "webmail.py": "non-CI journey (needs the mail profile: demo stack has no SOGo)",
    "saga_onboarding.py": "non-CI journey (needs OpenCloud + SOGo; run via `make saga`)",
    "saga_quote_to_cash.py": "non-CI journey (needs invoice + paperless overlays; run via `make saga`)",
    "saga_leave_request.py": "non-CI journey (needs Operaton; run via `make saga`)",
    "saga_support_ticket.py": "non-CI journey (needs ticketing overlay; run via `make saga`)",
    "saga_publishing.py": "non-CI journey (needs CMS overlay; run via `make saga`)",
    "saga_team_knowledge.py": "non-CI journey (needs notes overlay; run via `make saga`)",
}

# Scripts that are pure support / not standalone journeys. A script counts as a
# journey if it has a runnable `if __name__ == "__main__"` entrypoint.
SUPPORT = {"ai_mock.py", "cloud_mock.py", "cloud_mock_support.py", "run.py"}


def rendered_steps(wf: Path) -> list[str]:
    """Return every tests/05-e2e/<name>.py referenced by a workflow file."""
    if not wf.exists():
        return []
    text = wf.read_text()
    return sorted(set(re.findall(r"tests/05-e2e/([A-Za-z0-9_]+\.py)", text)))


def is_journey(fname: str) -> bool:
    """A script is a journey if it has a __main__ block and is not a support
    module."""
    if fname in SUPPORT:
        return False
    text = (E2E / fname).read_text()
    return "if __name__" in text


def check(room):
    """Run the AS6 gate; strict depends on AGENTIC_STRICT. Returns exit code."""
    strict = os.environ.get("AGENTIC_STRICT", "").lower() in {"1", "true", "yes"}

    journeys = [f for f in sorted(p.name for p in E2E.glob("*.py")) if is_journey(f)]
    rendered = set(rendered_steps(CI)) | set(rendered_steps(MAILCOW))

    # 1. phantom references: a CI step points at a missing script → defect.
    phantom = sorted(r for r in rendered if not (E2E / r).exists())
    for p in phantom:
        (room.fail if strict else room.info)(f"CI references missing script: {p}")

    # 2. unregistered journeys: a real journey with no CI step (unless allowlisted).
    unreg = sorted(j for j in journeys if j not in rendered and j not in ALLOWLIST)
    for j in unreg:
        (room.fail if strict else room.info)(
            f"e2e journey not registered in CI: {j} (add a CI step or allowlist)")

    ok = (not strict) or (not phantom and not unreg)

    if strict:
        room.info(f"strict mode: {len(journeys)} journeys, "
                  f"{len(journeys) - len(unreg)} in CI, {len(phantom)} phantom")
        for j in journeys:
            if j in rendered:
                room.ok(f"{j}: registered in CI")
    else:
        allowlisted = sorted(ALLOWLIST)
        room.info(f"informational mode (AGENTIC_STRICT=0): exit is always 0; "
                  f"set AGENTIC_STRICT=1 to enforce")
        for j in journeys:
            if j in rendered:
                room.ok(f"{j}: registered in CI")
            elif j in ALLOWLIST:
                room.info(f"allowlisted: {j} — {ALLOWLIST[j]}")
            else:
                room.info(f"unregistered: {j} (add a CI step or allowlist)")

    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="AS6 agent self-maintenance gate (opt-in)")
    ap.add_argument("--strict", action="store_true",
                    help="enforce the pyramid (same as AGENTIC_STRICT=1)")
    args = ap.parse_args()
    if args.strict:
        os.environ["AGENTIC_STRICT"] = "1"

    result = Result("agentic-self-maintenance")
    result.header("openSME AS6 agent self-maintenance gate (opt-in, not default)")
    code = check(result)
    print()
    result.summary()
    return code


if __name__ == "__main__":
    sys.exit(main())
