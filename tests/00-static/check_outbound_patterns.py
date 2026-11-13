#!/usr/bin/env python3
"""
tests/00-static/check_outbound_patterns.py — Layer 0 guard on the outbound
LLM payload-hygiene pattern catalog.

`tests/06-security/llm_outbound.py` (AE1) proves that the dev-maintenance-bot
never ships secrets, private IPs, absolute user paths or internal hostnames to
the LLM backend. That test needs a running bot + mock backend, so a typo that
silently disables one of its regexes would only surface in the nightly E2E
run — or never, if the E2E stack is broken.

This check gives the catalog teeth at Layer 0: it imports the same pattern
objects and asserts each one still matches a positive sample and still ignores
benign text. Weakening, deleting or inverting a pattern turns this red in
milliseconds, on any fresh checkout, with no docker.

Usage: python3 tests/00-static/check_outbound_patterns.py
"""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

MODULE = ROOT / "tests" / "06-security" / "llm_outbound.py"

# (attribute, positive samples, negative samples)
CASES = [
    (
        "SECRET_PATTERNS",
        [
            '{"password": "hunter2secret"}',
            '{"api_key": "sk-abcdef123456"}',
            "token = 'ghp_abcdefgh1234'",
        ],
        [
            '{"message": "hello world"}',
            '{"status": "ok"}',
        ],
    ),
    (
        "PRIVATE_IP_RE",
        ["10.1.2.3", "172.19.0.250", "192.168.1.1", "127.0.0.1"],
        ["8.8.8.8", "195.90.216.140"],
    ),
    (
        "ABS_PATH_RE",
        ["/home/alice/project", "/root/git/repo"],
        ["/api/v1/status", "/var/lib/data"],
    ),
    (
        "INTERNAL_HOST_RE",
        ["zitadel.internal", "postgres", "stalwart.local"],
        ["example.org", "graphwiz.ai"],
    ),
]


def load_module():
    spec = importlib.util.spec_from_file_location("llm_outbound", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(room: Result) -> int:
    room.header(
        "Layer 0: outbound LLM payload patterns — catalog must still detect "
        "secrets, private IPs, user paths and internal hosts"
    )

    if not MODULE.is_file():
        room.fail(f"pattern catalog missing: {MODULE.relative_to(ROOT)}")
        return 1

    try:
        mod = load_module()
    except Exception as exc:  # noqa: BLE001 - report, don't crash the suite
        room.fail(f"cannot import {MODULE.relative_to(ROOT)}: {exc!r}")
        return 1

    for attr, positives, negatives in CASES:
        pattern = getattr(mod, attr, None)
        if pattern is None:
            room.fail(f"{attr}: not defined in {MODULE.relative_to(ROOT)}")
            continue
        if not hasattr(pattern, "search"):
            room.fail(f"{attr}: is not a compiled regex")
            continue

        misses = [s for s in positives if not pattern.search(s)]
        if misses:
            room.fail(f"{attr}: fails to match {misses!r}")
        else:
            room.ok(f"{attr}: matches {len(positives)} positive sample(s)")

        leaks = [s for s in negatives if pattern.search(s)]
        if leaks:
            room.fail(f"{attr}: false-positive on benign {leaks!r}")
        else:
            room.ok(f"{attr}: ignores {len(negatives)} benign sample(s)")

    return 0 if room.summary() else 1


def main() -> int:
    return check(Result("outbound-payload-patterns"))


if __name__ == "__main__":
    sys.exit(main())
