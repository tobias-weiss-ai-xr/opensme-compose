#!/usr/bin/env python3
"""
tests/00-static/check_env_quote.py — Layer 0 guard for the dotenv quoting
invariant.

Proven incident (T1): scripts/brand.sh wrote an UNQUOTED multi-word value
into /opt/opensme-compose/compose/.env (PORTAL_TAGLINE=open Source Managed
Enterprise). A later `source .env` in scripts/calendar-setup.sh assigned
PORTAL_TAGLINE=open and tried to EXECUTE the command `Source` ->
'Source: command not found' aborted the script.

This check pins three invariants:

  1. brand.sh quote invariant: every .env write path emits values
     double-quoted (KEY="value with spaces"). An unquoted emission
     (printf '%s=%s' / a sed replacement ${key}=${val}) fails the check.
  2. Loader fixture: the hardened loader (the env-quote-hardening block,
     byte-identical in calendar-setup.sh and demo-data.sh) is extracted
     and run against a fixture env file with 'FOO=multi word value':
     the load must not execute value words as commands (no 'command not
     found' on stderr), FOO must keep the full value, the file must be
     rewritten quoted IN PLACE with a .bak-<date> backup, a warning
     naming file+line must be emitted, and a second run on the now-clean
     file must be a no-op (idempotent).
  3. Hardening marker: both loader scripts must carry the
     env-quote-hardening block and route their dotenv loads through
     env_quote_load (a loader that goes back to plain `source` fails).

Pure file inspection + a throwaway bash fixture under TMPDIR — no docker,
no sudo, runs on any fresh checkout.

Usage: python3 tests/00-static/check_env_quote.py
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

BRAND = ROOT / "scripts" / "brand.sh"
LOADER_SCRIPTS = {
    "calendar-setup.sh": ROOT / "scripts" / "calendar-setup.sh",
    "demo-data.sh": ROOT / "scripts" / "demo-data.sh",
}

MARK_BEGIN = "# env-quote-hardening:BEGIN"
MARK_END = "# env-quote-hardening:END"

# brand.sh emission paths (set_env): sed replacement + printf append.
# Either reverting to unquoted emission must fail this check.
UNQUOTED_EMISSIONS = (
    ("unquoted printf emission", re.compile(r"printf '%s=%s\\n'")),
    ("unquoted sed replacement", re.compile(r"\|\$\{key\}=\$\{val\}\|")),
)
QUOTED_REQUIRED = (
    ("quoted printf emission", "printf '%s=\"%s\"\\n'"),
    ("quoted sed replacement", '${key}=\\"${val}\\"'),
)

# each loader script must route its dotenv load through the hardened loader
LOADER_CALLS = {
    "calendar-setup.sh": 'env_quote_load "$ENV_FILE"',
    "demo-data.sh": 'env_quote_load "$_env_copy"',
}

# fixture: one normal line, one unquoted multi-word value (the incident
# shape), one value whose second word is a real command name — before the
# hardening, `source` assigned FOO=multi and EXECUTED the word `Source`;
# EVIL exercises the same failure mode with `rm` as the stray command.
FIXTURE = (
    "# comment line stays untouched\n"
    "BAR=simple\n"
    "FOO=multi word value\n"
    "EVIL=rm -rf /tmp/opensme-env-quote-must-not-run\n"
)


def extract_block(text: str) -> str:
    """The env-quote-hardening block, or '' when the markers are gone."""
    start = text.find(MARK_BEGIN)
    end = text.find(MARK_END)
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start + len(MARK_BEGIN):end].strip("\n")


def run_loader_harness(block: str, fixture: Path) -> subprocess.CompletedProcess:
    """Extract the loader block into a throwaway bash script and run it
    against the fixture under `set -a` (same semantics as the real load)."""
    harness = "\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        block,
        "set -a",
        f'env_quote_load "{fixture}" fixture.env',
        "set +a",
        "printf 'FOO=[%s]\\n' \"${FOO:-<unset>}\"",
        "printf 'BAR=[%s]\\n' \"${BAR:-<unset>}\"",
        "printf 'EVIL=[%s]\\n' \"${EVIL:-<unset>}\"",
        "",
    ])
    hpath = fixture.parent / "harness.sh"
    hpath.write_text(harness)
    return subprocess.run(
        ["bash", str(hpath)], capture_output=True, text=True, timeout=60
    )


def fixture_run_failures(name: str, block: str) -> list[str]:
    """Run the extracted loader against a fresh fixture and return every
    violated invariant as a failure message."""
    bad: list[str] = []
    with tempfile.TemporaryDirectory(prefix="opensme-env-quote-") as td:
        fixture = Path(td) / "fixture.env"
        fixture.write_text(FIXTURE)

        try:
            proc = run_loader_harness(block, fixture)
        except subprocess.TimeoutExpired:
            return [f"{name}: loader harness timed out on the fixture"]

        out, err = proc.stdout, proc.stderr
        if proc.returncode != 0:
            bad.append(
                f"{name}: loader DIED on the fixture (rc={proc.returncode}) — "
                "the loader must never abort on an unquoted multi-word line "
                f"(stderr: {err.strip()[:200]!r})"
            )
        if "command not found" in err:
            bad.append(
                f"{name}: loader EXECUTED value words as commands "
                "('command not found' on stderr) — the unquoted-value "
                "incident is live again"
            )
        if "FOO=[multi word value]" not in out:
            bad.append(
                f"{name}: FOO lost its full multi-word value "
                f"(stdout: {out.strip()[:200]!r}) — expected "
                "'FOO=[multi word value]'"
            )
        if "BAR=[simple]" not in out:
            bad.append(f"{name}: normal line BAR broken by the scrub (stdout: {out.strip()[:200]!r})")
        if "EVIL=[rm -rf /tmp/opensme-env-quote-must-not-run]" not in out:
            bad.append(f"{name}: EVIL multi-word value not preserved (stdout: {out.strip()[:200]!r})")
        if 'FOO="multi word value"' not in fixture.read_text():
            bad.append(
                f"{name}: loader did not rewrite the unquoted line quoted "
                "IN PLACE in the fixture file"
            )
        if not list(fixture.parent.glob("fixture.env.bak-*")):
            bad.append(f"{name}: loader produced no .bak-<date> backup of the fixture")
        if not re.search(r"fixture\.env:[0-9]+", err):
            bad.append(
                f"{name}: no warning naming file+line for the offending "
                "fixture line (expected 'fixture.env:<line>' on stderr)"
            )

        # idempotency: re-running on the now-quoted file must be a no-op
        baks_before = set(fixture.parent.glob("*.bak-*"))
        try:
            proc2 = run_loader_harness(block, fixture)
        except subprocess.TimeoutExpired:
            return bad + [f"{name}: loader harness timed out on the second run"]
        if proc2.returncode != 0:
            bad.append(f"{name}: loader died re-loading an already-quoted fixture (rc={proc2.returncode})")
        if "command not found" in proc2.stderr:
            bad.append(f"{name}: second load EXECUTED value words ('command not found' on stderr)")
        if "env-quote" in proc2.stderr:
            bad.append(
                f"{name}: loader is not idempotent — warned again on a "
                "clean (already quoted) fixture"
            )
        if set(fixture.parent.glob("*.bak-*")) != baks_before:
            bad.append(
                f"{name}: loader is not idempotent — wrote another backup "
                "on a clean (already quoted) fixture"
            )
        if "FOO=[multi word value]" not in proc2.stdout:
            bad.append(f"{name}: second load lost the FOO value (stdout: {proc2.stdout.strip()[:200]!r})")
    return bad


def main() -> int:
    result = Result("env-quote-hardening")
    result.header("Layer 0: env-quote hardening (brand.sh emission + dotenv loaders)")

    failures: list[str] = []

    # ── 1. brand.sh quote invariant ────────────────────────────────────
    brand = BRAND.read_text()
    for what, rx in UNQUOTED_EMISSIONS:
        if rx.search(brand):
            failures.append(
                f"brand.sh writes UNQUOTED env values ({what}) — every "
                "set_env emission must be KEY=\"value\" or `source .env` "
                "will execute the second word of a multi-word value "
                "('Source: command not found' incident)"
            )
    for what, needle in QUOTED_REQUIRED:
        if needle not in brand:
            failures.append(
                f"brand.sh lost the {what} in set_env — the hardened "
                "emission path (KEY=\"value\") was weakened or removed"
            )

    # ── 2 + 3. hardening markers, call sites, byte-identical blocks ────
    blocks: dict[str, str] = {}
    for name, path in LOADER_SCRIPTS.items():
        text = path.read_text()
        blocks[name] = extract_block(text)
        if not blocks[name]:
            failures.append(
                f"{name} lost the env-quote-hardening block ({MARK_BEGIN}…"
                f"{MARK_END}) — dotenv loads are unguarded again"
            )
        if LOADER_CALLS[name] not in text:
            failures.append(
                f"{name} no longer routes its dotenv load through "
                f"env_quote_load (expected {LOADER_CALLS[name]!r}) — a "
                "plain `source` of a hostile .env aborts the script"
            )
    if len(set(blocks.values())) > 1:
        failures.append(
            "env-quote-hardening blocks diverged between "
            + " and ".join(LOADER_SCRIPTS)
            + " — the block is contractually byte-identical (extracted and "
            "exercised per script by this check)"
        )

    # ── loader fixture: extract, run, assert behavior ──────────────────
    if all(blocks.values()):
        for name, block in blocks.items():
            for f in fixture_run_failures(name, block):
                failures.append(f)

    if failures:
        for f in failures:
            result.fail(f)
        result.info(
            "invariants: brand.sh emissions quoted; loader scrubs unquoted "
            "multi-word values (in place, .bak-<date>, file+line warning), "
            "never dies, idempotent"
        )
    else:
        result.ok(
            "brand.sh emits quoted env values; both loaders scrub, warn "
            "(file+line), back up, never die, and stay idempotent"
        )
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
