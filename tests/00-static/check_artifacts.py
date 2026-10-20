#!/usr/bin/env python3
"""
tests/00-static/check_artifacts.py — Layer 0 guard against committed runtime artifacts.

Runtime artifacts — backup tarballs, database dumps, deploy-provisioned
secrets — belong to a running deployment, never to the repository. They
bloat every clone and, in the worst case, publish credentials: a
`scripts/backup.sh` volume tarball carries the Zitadel machine key, the
login-client PAT and the OpenCloud config.

`scan_secrets.py` only reads text files, so a gzip/tar payload is invisible
to it. This check works on the *tracked-file list* (`git ls-files`) instead
and fails on any path that must never be committed, then confirms the
ignore rule is in place so a fresh `git add -A` cannot re-add them.

Usage: python3 tests/00-static/check_artifacts.py
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

# Tracked path matching (regex) → why it must never be committed.
FORBIDDEN = [
    (r"(^|/)backups/", "runtime backup output (scripts/backup.sh)"),
    (r"\.(sql\.gz|dump|tar\.gz|tgz|tar\.bz2|tar\.xz)$",
     "database dump / binary archive"),
    (r"(^|/)idm/secrets/(?!\.gitkeep$)", "deploy-provisioned secret"),
    (r"\.(pem|key|p12|pfx)$", "private key / keystore"),
    (r"(^|/)\.env(\.[^/]*)?$",
     "real env file (only .env.example / .env.demo may ship)"),
]

# Tracked names that are templates by design, not live secrets.
ALLOWED = {".env.example", ".env.demo"}

# Ignore probes: (label, path) that must stay ignored so `git add -A` is safe.
IGNORE_PROBES = [
    ("backups/", "backups/probe.sql.gz"),
    (".env", ".env"),
    ("idm/secrets/", "idm/secrets/masterkey"),
    ("certs/", "certs/probe.pem"),
]


def tracked_files() -> list[str] | None:
    """Tracked paths, or None when git is unavailable / not a checkout."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.split("\0") if p]


def is_ignored(path: str) -> bool:
    proc = subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=str(ROOT),
        capture_output=True, text=True, timeout=30)
    return proc.returncode == 0


def main() -> int:
    result = Result("repo-artifacts")
    result.header("Layer 0: committed-artifact hygiene")

    files = tracked_files()
    if files is None:
        result.skip("not a git checkout — cannot list tracked files")
        result.summary()
        return 0

    violations: list[tuple[str, str]] = []
    for path in files:
        if Path(path).name in ALLOWED:
            continue
        for pattern, why in FORBIDDEN:
            if re.search(pattern, path):
                violations.append((path, why))
                break

    if violations:
        for path, why in violations:
            result.fail(f"{path} — {why}")
    else:
        result.ok(f"no runtime artifacts among {len(files)} tracked files")

    for label, probe in IGNORE_PROBES:
        if is_ignored(probe):
            result.ok(f"{label} is gitignored")
        else:
            result.fail(
                f"{label} is not gitignored — "
                "a `git add -A` could commit secrets"
            )

    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
