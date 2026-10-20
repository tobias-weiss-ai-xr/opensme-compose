#!/usr/bin/env python3
"""
tests/00-static/check_suite_coverage.py — Drift guard for the test suite itself.

Guards against the silent-dead-check failure mode: a documented Layer-0
guard that the runner never executes. Concretely it verifies:

  1. Every `tests/00-static/check_*.py` (except the deliberately opt-in
     agentic gate, documented as NON-DEFAULT) is registered in the
     Layer-0 script list of `tests/run.py`. A check that exists but is
     never run is worse than no check — the suite reports green while the
     invariant it guards silently rots (seen 2026-10-16: check_agent.py
     and check_backup.py were documented as Layer-0 / in-CI but the
     runner never invoked them).
  2. Every `tests/00-static/check_*.py` referenced from docs/VALIDATION.md
     is either in the runner or on the documented opt-in allowlist.
  3. Every `tests/00-static/check_*.py` referenced from Makefile test
     targets is in the runner (a Makefile-only reference that the runner
     drops is a coverage hole).
  4. The opt-in allowlist (agentic gate) always carries a reason and is
     visible in VALIDATION.md.

Usage:  python3 tests/00-static/check_suite_coverage.py
Exit:   0 pass, 1 fail. Wired into tests/run.py Layer 0.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

ROOT = Path(__file__).resolve().parents[2]

# Deliberately OPT-IN checks: they pass only under a strict flag / as an
# operator gate and must NOT become part of the default suite. Any check on
# this list must have a visible reason and a doc reference.
OPT_IN = {
    "check_agentic.py": (
        "AS6 agent self-maintenance gate — AGENTIC_STRICT=1 opt-in by design "
        "(docstring + docs/AGENTIC-AI.md §7.6); running it always would "
        "force the operator-only push/deploy decision into every commit."
    ),
}


def wired_runner_checks() -> set[str]:
    """Set of tests/00-static/check_*.py names registered in run.py Layer 0.

    Layer 0 is the first block with "requires_stack": False under "Static
    validation". We scan the whole run.py for script path strings — being
    conservative (any reference counts as wired) keeps false positives out.
    """
    text = (ROOT / "tests" / "run.py").read_text()
    return set(re.findall(r"tests/00-static/(check_[A-Za-z0-9_]+\.py)", text))


def validation_doc_disk_checks() -> set[str]:
    """Every check_*.py named in docs/VALIDATION.md."""
    doc = (ROOT / "docs" / "VALIDATION.md")
    if not doc.exists():
        return set()
    return set(re.findall(r"tests/00-static/(check_[A-Za-z0-9_]+\.py)", doc.read_text()))


def makefile_checks() -> set[str]:
    """Every check_*.py named in the Makefile."""
    mk = (ROOT / "Makefile")
    if not mk.exists():
        return set()
    return set(re.findall(r"tests/00-static/(check_[A-Za-z0-9_]+\.py)", mk.read_text()))


_IV_START = "<!-- CHECK-INVENTORY-BEGIN -->"
_IV_END = "<!-- CHECK-INVENTORY-END -->"


def _inventory_block(doc_text: str) -> set[str]:
    """Names of check_*.py inside the machine-readable inventory fence of
    docs/VALIDATION.md. Files not inside the fence are invisible to the
    check (prose references are unstable)."""
    start = doc_text.find(_IV_START)
    end = doc_text.find(_IV_END)
    if start == -1 or end == -1 or end <= start:
        return set()
    return set(
        re.findall(r"check_[A-Za-z0-9_]+\.py", doc_text[start:end])
    )


def main() -> int:
    result = Result("sme-suite-coverage")
    result.header("Layer 0: test-suite coverage (no silently-dead checks)")

    failures = []
    info: list[str] = []

    disk = sorted(p.name for p in (ROOT / "tests" / "00-static").glob("check_*.py"))
    wired = wired_runner_checks()
    doc = validation_doc_disk_checks()
    mk = makefile_checks()

    # ── 0. CI runs the full suite, not a drift-prone subset ────────────
    # The `static` job must invoke `tests/run.py --static` (the single
    # entry point that owns the layer-0 list). If CI were to call the
    # check scripts individually, adding a check to run.py would silently
    # not protect the release, recreating the dead-check bug at the CI
    # layer.
    ci = (ROOT / ".github" / "workflows" / "ci.yml")
    if ci.exists():
        ci_text = ci.read_text()
        if "tests/run.py --static" not in ci_text and "tests/run.py --layer 0" not in ci_text:
            failures.append(
                "CI's static job must call `tests/run.py --static` (single "
                "entry point); invoking layer-0 check scripts individually "
                "lets a newly added check silently drop out of the gate"
            )
        # Single-entry-point guard: CI must NOT call any tests/00-static/*.py
        # directly. The runner (`tests/run.py --static`) owns the layer-0
        # script list; a direct individual call means adding a check to run.py
        # silently does not protect the release, recreating the dead-check bug
        # at the CI layer (a stale, self-pinned check list).
        direct_calls = re.findall(r"tests/00-static/([A-Za-z0-9_]+\.py)", ci_text)
        if direct_calls:
            failures.append(
                "CI static job must not call tests/00-static/"
                f"{direct_calls[0]} directly — tests/run.py --static is the "
                "single entry point; a direct call recreates the dead-check "
                "bug at the CI layer"
            )

    # ── 1. every check on disk is run or documented-opt-in ─────────────
    unknown = [f for f in disk if f not in wired and f not in OPT_IN]
    if unknown:
        failures.append(
            "checks exist but are never run (wire into tests/run.py Layer 0 "
            f"or document as opt-in): {', '.join(unknown)}"
        )

    # ── 2. documented-as-CI checks must be wired ────────────────────────
    # VALIDATION.md marks layer-0 checks "In CI" — if one is doc'd there but
    # not run, the doc is a lie and the invariant is unprotected.
    doc_unwired = sorted(f for f in doc if f not in wired and f not in OPT_IN)
    if doc_unwired:
        failures.append(
            "docs/VALIDATION.md presents these as covered but they are not "
            f"in the runner: {', '.join(doc_unwired)}"
        )

    # ── 3. Makefile-referenced checks must be in the runner ─────────────
    mk_unwired = sorted(f for f in mk if f not in wired and f not in OPT_IN)
    mk_phantom = sorted(f for f in mk_unwired if f not in disk)
    mk_missing = sorted(f for f in mk_unwired if f in disk)
    if mk_phantom:
        failures.append(
            "Makefile references checks that do not exist on disk: "
            f"{', '.join(mk_phantom)} (check the target's script path)"
        )
    if mk_missing:
        failures.append(
            "Makefile targets invoke checks the runner drops: "
            f"{', '.join(mk_missing)} — a `make lint`/`make test-static` "
            "run would not be reproducible via tests/run.py --static"
        )

    # ── 4. opt-in entries stay small, documented, and reason-carrying ──
    for f, why in sorted(OPT_IN.items()):
        if f not in disk:
            failures.append(f"OPT_IN lists {f} but no such check exists on disk")
        if len(why) < 40:
            failures.append(f"OPT_IN entry for {f} lacks a visible reason")
    if OPT_IN and "check_agentic" not in (ROOT / "docs" / "VALIDATION.md").read_text():
        info.append(
            "check_agentic.py is opt-in — consider mirroring that in "
            "docs/VALIDATION.md so readers know it is not part of --static"
        )

    # ── 5. wired checks are documented in VALIDATION.md ────────────────
    # A check that runs but is invisible in the release doc hides its
    # guarantee; readers of "Run everything locally" would not know the
    # suite now covers it. Opt-in gates are exempt (documented elsewhere).
    # The doc carries a machine-readable inventory block (see below); we
    # match against that block only so the check stays robust to prose.
    doc_text = (ROOT / "docs" / "VALIDATION.md").read_text()
    inventory_block = _inventory_block(doc_text)
    undocumented = sorted(
        f for f in wired if f not in OPT_IN and f not in inventory_block
    )
    if undocumented:
        failures.append(
            "wired runner checks missing from the docs/VALIDATION.md check "
            f"inventory: {', '.join(undocumented)}"
        )
    # and nothing in the inventory may list a check that does not exist.
    phantom = sorted(f for f in inventory_block if f not in disk)
    if phantom:
        failures.append(
            "docs/VALIDATION.md inventory lists checks that do not exist "
            f"on disk: {', '.join(phantom)}"
        )

    for f in sorted(wired):
        info.append(f"{f}: wired in tests/run.py Layer 0")
    for f in sorted(OPT_IN):
        info.append(f"{f}: documented opt-in (not default)")

    # ── 6. the Makefile `saga` target covers every saga journey ─────────
    # `make saga` must run every saga_*.py that is a journey (has a __main__
    # block); dropping one silently shrinks the local journey fleet. The
    # list is authored once in the Makefile; add new journeys there or get a
    # red here.
    _saga_loop = re.findall(
        r"for script in ([^;]+); do", (ROOT / "Makefile").read_text()
    )
    saga_listed = set()
    for grp in _saga_loop:
        # tokens are `saga_<name>` basenames (script names without the .py
        # suffix, per the existing loop body `$(PYTHON) tests/05-e2e/"$$script.py"`).
        for tok in re.split(r"[\\\s]+", grp.strip()):
            tok = tok.strip()
            if re.fullmatch(r"saga_[A-Za-z0-9_]+", tok):
                saga_listed.add(tok + ".py")
    saga_on_disk = sorted(
        p.name for p in (ROOT / "tests" / "05-e2e").glob("saga_*.py")
        if "if __name__" in p.read_text()
    )
    saga_missing = sorted(f for f in saga_on_disk if f not in saga_listed)
    if saga_missing:
        failures.append(
            "Makefile `saga` target omits journeys present in tests/05-e2e: "
            f"{', '.join(saga_missing)} (add them to the for-script list)"
        )

    # ── 7. the default `run.py` covers every implemented stack layer ────
    # Layers 3 (smoke) and 4 (integration) are both stack-gated but
    # self-skipping (WARN + skip when the stack is down / a service is
    # absent); leaving 4 out of the default run silently drops all
    # cross-wiring contracts (seen live: the default run skipped layer 4
    # with a stale "4 unimplemented" comment years after 3777151 added it).
    runpy = (ROOT / "tests" / "run.py").read_text()
    layer4_exists = bool(re.search(r"^\s*4: \{", runpy, re.M))
    if layer4_exists:
        # last [0,N,...] list assignment is the else/default branch
        lists = re.findall(r"layers_to_run = \[([0-9, ]+)\]", runpy)
        if lists:
            default_list = [int(x) for x in lists[-1].split(",") if x.strip()]
            if 4 not in default_list:
                failures.append(
                    "tests/run.py default run omits layer 4 (integration) even "
                    "though it is implemented and self-skips like layer 3 — "
                    "cross-wiring contracts would silently never run"
                )
    # --domain must reach integration with the resolved value (not a
    # hard-coded default) OR be dropped silently; check for the explicit
    # marker pairs that encode both properties.
    # The --domain → integration hand-off must be present as a unit.
    # Three independent markers are required together; requiring them all
    # (rather than keying off one of them) keeps the check active even when
    # a contributor deletes the whole branch. Every --domain run must
    # smoke-test and integration-validate the SAME host.
    _fwd_markers = (
        'domain_explicit = "--domain" in sys.argv',
        'elif "integration" in script_path and domain_explicit',
        '["--domain", domain]',
    )
    missing_fwd = [m for m in _fwd_markers if m not in runpy]
    if missing_fwd:
        failures.append(
            "tests/run.py --domain forwarding to integration is broken "
            f"(missing: {', '.join(missing_fwd)}) — a --domain run would "
            "validate smoke and integration wiring against different hosts "
            "(silent skew)"
        )

    if failures:
        for f in failures:
            result.fail(f)
    else:
        result.ok(
            f"suite coverage complete: {len(disk)} checks, "
            f"{len(wired)} wired, {len(OPT_IN)} documented opt-in, "
            f"doc + Makefile consistent"
        )
    for i in info:
        result.info(i)
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
