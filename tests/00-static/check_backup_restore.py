#!/usr/bin/env python3
"""
tests/00-static/check_backup_restore.py — Layer 0 stack-free guard on the
backup/restore contract.

backup.sh and restore.sh form a round-trip contract: every artifact
backup.sh *writes* (postgres_*, traefik_*, volumes_*) must be *applied* by
restore.sh, or a backup is produced that can never come back. The classic
regression is a new artifact written by backup.sh that restore.sh never
touches (or only echoes in --list).

This check pins that contract with pure file inspection + `bash -n` — no
docker, no e2e — so it runs on any fresh checkout:

  1. backup.sh / restore.sh exist, are executable, use the bash shebang,
     contain `set -euo pipefail`, and pass `bash -n`.
  2. backup.sh writes all three artifact families and keeps KNOWN_VOLUMES.
  3. restore.sh references every artifact AND applies it (not merely lists
     it in --list), handles --list/--dry-run/--pg-only/--volumes-only, and
     validates the combined volume tar loudly before extracting.
  4. The footgun stays documented: backup.sh must not `compose down` before
     the SQL/traefik dumps (dump first, stop later).
  5. Retention (7 days) and log appends (backup.log / restore.log).

Usage: python3 tests/00-static/check_backup_restore.py
"""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT  # noqa: E402

BACKUP = ROOT / "scripts" / "backup.sh"
RESTORE = ROOT / "scripts" / "restore.sh"

SHEBANG = "#!/usr/bin/env bash"
PIPEFAIL = "set -euo pipefail"

# Artifact families backup.sh writes. Each must be applied by restore.sh.
ARTIFACTS = ("postgres_", "traefik_", "volumes_")

# One apply-marker grep per artifact: proves restore.sh actually feeds the
# produced archive back in, instead of only echoing the name in --list.
APPLY_MARKERS = {
    "postgres_": re.compile(r'gunzip\s+-c\s+"\$PG_FILE"'),
    "traefik_": re.compile(r'tar\s+-x\S*f\s+"\$TRAEFIK_FILE"'),
    "volumes_": re.compile(r'tar\s+-x\S*f\s+"\$VOLUMES_FILE"'),
}

# restore.sh must validate the combined volume tar loudly before extracting.
LOUD_VOLUME_TAR = re.compile(r'!\s+tar\s+-x\S*f\s+"\$VOLUMES_FILE"')
LOUD_VOLUME_MSG = re.compile(r"not a valid tar")

# Dump first, stop later: the SQL/traefik dump machinery and artifact writes
# must all precede the first real `docker compose down` (the compose commands
# live on their own lines inside multi-line pipelines, so each marker is a
# single-line regex).
PG_DUMP_CMD = re.compile(r"pg_dumpall")
TRAEFIK_DUMP_CMD = re.compile(r"tar czf")
PG_ARTIFACT_WRITE = re.compile(r'postgres_\$\{TIMESTAMP\}\.sql\.gz')
TRAEFIK_ARTIFACT_WRITE = re.compile(r'traefik_\$\{TIMESTAMP\}\.tar\.gz')
FIRST_DOWN = re.compile(r"docker compose down")
FOOTGUN_COMMENT = re.compile(r"silently EMPTY")

RESTORE_FLAGS = ("--list", "--dry-run", "--pg-only", "--volumes-only")
RETENTION = re.compile(r"mtime\s+\+7")


def read_lines(path: Path) -> list[str] | None:
    """File as a list of lines, or None when missing/unreadable."""
    try:
        return path.read_text().splitlines()
    except OSError:
        return None


def grep_hit(lines: list[str], pattern: str | re.Pattern) -> bool:
    """True if any line matches `pattern` (substring or compiled regex)."""
    if isinstance(pattern, str):
        return any(pattern in line for line in lines)
    return any(pattern.search(line) for line in lines)


def first_match(lines: list[str], pattern: str | re.Pattern) -> int | None:
    """1-based line number of the first matching line, or None."""
    if isinstance(pattern, str):
        for idx, line in enumerate(lines, start=1):
            if pattern in line:
                return idx
    else:
        for idx, line in enumerate(lines, start=1):
            if pattern.search(line):
                return idx
    return None


def shell_syntax_ok(path: Path) -> tuple[bool, str]:
    """Run `bash -n`; return (syntax_ok, stderr-or-empty)."""
    try:
        proc = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "bash not available in this environment"
    return proc.returncode == 0, (proc.stderr or "").strip()


def main() -> int:
    result = Result("backup-restore")
    result.header("Layer 0: backup/restore contract (stack-free)")

    backup_lines = read_lines(BACKUP)
    restore_lines = read_lines(RESTORE)

    # ── 1. Script fundamentals ──────────────────────────────
    for name, path, lines in (("backup.sh", BACKUP, backup_lines),
                              ("restore.sh", RESTORE, restore_lines)):
        rel = path.relative_to(ROOT)
        if lines is None:
            result.fail(f"{rel} is missing or unreadable")
            continue
        if os.access(path, os.X_OK):
            result.ok(f"{rel} is executable")
        else:
            result.fail(f"{rel} is not executable")
        if lines and lines[0].strip() == SHEBANG:
            result.ok(f"{rel} starts with the bash shebang")
        else:
            result.fail(f"{rel} must start with '{SHEBANG}'")
        if grep_hit(lines, PIPEFAIL):
            result.ok(f"{rel} contains 'set -euo pipefail'")
        else:
            result.fail(f"{rel} must contain 'set -euo pipefail'")
        # ── 2. bash -n ─────────────────────────────────────
        ok, err = shell_syntax_ok(path)
        if ok:
            result.ok(f"{rel} passes bash -n")
        else:
            result.fail(f"{rel} fails bash -n: {err}")

    if backup_lines is None or restore_lines is None:
        return 0 if result.summary() else 1

    # ── 3. backup.sh artifact contract ─────────────────────
    result.header("backup.sh artifact contract")
    for art in ARTIFACTS:
        if grep_hit(backup_lines, art):
            result.ok(f"backup.sh writes the {art}<ts> artifact")
        else:
            result.fail(f"backup.sh no longer writes the {art}<ts> artifact")
    if grep_hit(backup_lines, "KNOWN_VOLUMES"):
        result.ok("backup.sh keeps the documented KNOWN_VOLUMES list")
    else:
        result.fail("backup.sh lost its KNOWN_VOLUMES volume list")

    # ── 5. Footgun: dump first, stop later ─────────────────
    result.header("backup-before-stop footgun")
    if grep_hit(backup_lines, FOOTGUN_COMMENT):
        result.ok("backup.sh keeps the 'silently EMPTY' footgun documented")
    else:
        result.fail("backup.sh dropped the 'silently EMPTY' footgun note")
    down_pos = first_match(backup_lines, FIRST_DOWN)
    dump_positions = [
        first_match(backup_lines, PG_DUMP_CMD),
        first_match(backup_lines, TRAEFIK_DUMP_CMD),
        first_match(backup_lines, PG_ARTIFACT_WRITE),
        first_match(backup_lines, TRAEFIK_ARTIFACT_WRITE),
    ]
    if (down_pos is not None
            and all(p is not None and p < down_pos for p in dump_positions)):
        result.ok("backup.sh dumps SQL/traefik before any `compose down`")
    else:
        result.fail(
            "backup.sh must `compose down` only AFTER the SQL/traefik dumps "
            "(downing first makes the dumps silently EMPTY)"
        )

    # ── 6. Retention + logs ────────────────────────────────
    result.header("retention and logs")
    if grep_hit(backup_lines, RETENTION):
        result.ok("backup.sh keeps 7 days of backups (retention)")
    else:
        result.fail("backup.sh must keep 7 days of backups (mtime +7)")
    if grep_hit(backup_lines, "backup.log"):
        result.ok("backup.sh appends backup.log")
    else:
        result.fail("backup.sh must append to backup.log")
    if grep_hit(restore_lines, "restore.log"):
        result.ok("restore.sh appends restore.log")
    else:
        result.fail("restore.sh must append to restore.log")

    # ── 4. restore.sh symmetry with backup.sh ──────────────
    result.header("restore.sh symmetry (apply, not merely list)")
    for art in ARTIFACTS:
        if grep_hit(restore_lines, art):
            result.ok(f"restore.sh references the {art}<ts> artifact")
        else:
            result.fail(
                f"restore.sh has no reference to {art} — a {art}<ts> backup "
                "artifact could never be restored"
            )
        if grep_hit(restore_lines, APPLY_MARKERS[art]):
            result.ok(f"restore.sh APPLIES {art} (not merely a --list mention)")
        else:
            result.fail(
                f"restore.sh only mentions {art} — it must APPLY it, "
                "not just list it"
            )
    for flag in RESTORE_FLAGS:
        if grep_hit(restore_lines, flag):
            result.ok(f"restore.sh handles {flag}")
        else:
            result.fail(f"restore.sh dropped the {flag} flag")
    if (grep_hit(restore_lines, LOUD_VOLUME_TAR)
            and grep_hit(restore_lines, LOUD_VOLUME_MSG)):
        result.ok("restore.sh validates the combined volume tar loudly "
                  "before extracting")
    else:
        result.fail(
            "restore.sh must validate the combined volume tar (fail loudly, "
            "not silently pretend) before extracting"
        )

    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
