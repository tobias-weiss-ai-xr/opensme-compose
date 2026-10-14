#!/usr/bin/env python3
"""check_backup — every stateful volume the compose fragments ship must be
in scripts/backup.sh's KNOWN_VOLUMES (or explicitly excluded).

Why: the backup list is hand-maintained while volumes live in ~20 compose
files — the two drift apart silently (found live: the HITL audit trail
dev-maintenance-bot-state, the BPM history camunda-data, portal-data and
opencloud-lib were all missing from backups). This check makes the gap
class impossible to reintroduce: it parses the fragments itself and fails
on ANY difference against backup.sh.

Model caches (huge, re-downloadable) are the only allowed exclusions and
must be documented in backup.sh itself, so the list and this check cannot
drift independently.
"""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BACKUP_SH = ROOT / "scripts" / "backup.sh"

# Compose fragments that may define named volumes (same families the
# other static checks and compose_config.py enumerate).
FRAGMENT_GLOBS = [
    "docker-compose.yml",
    "idm/*.yml",
    "opencloud/*.yml",
    "mail/*.yml",
    "services/*.yml",
    "monitoring/*.yml",
]

# The ONLY volumes allowed to be absent from the backup list — and each
# must appear in a backup.sh comment, so the exclusion is documented where
# the operator reads it.
EXCLUDED = {
    "ai-models": "re-downloadable model cache",
    "ollama-models": "re-downloadable model cache",
}


def fragment_volumes():
    vols = {}
    for pattern in FRAGMENT_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            try:
                doc = yaml.safe_load(path.read_text())
            except Exception as e:  # yaml_lint covers syntax; be lenient here
                print(f"  ⚠ {path}: unparseable ({e})")
                continue
            vols_def = (doc or {}).get("volumes")
            if not isinstance(vols_def, dict):
                continue
            for name, spec in vols_def.items():
                if isinstance(spec, dict) and spec.get("external"):
                    continue  # external volumes are managed outside the stack
                vols.setdefault(name, []).append(str(path.relative_to(ROOT)))
    return vols


def backup_known_volumes():
    text = BACKUP_SH.read_text()
    block = re.search(r"KNOWN_VOLUMES=\(\n(.*?)\n\)", text, re.S)
    if not block:
        raise SystemExit("KNOWN_VOLUMES array not found in scripts/backup.sh")
    # strip comments BEFORE extracting names — the exclusion documentation
    # lives in comments inside the array and must not become entries
    body = re.sub(r"#[^\n]*", "", block.group(1))
    names = set(re.findall(r"([a-z0-9_-]+)", body))
    return names, text


def main():
    vols = fragment_volumes()
    if not vols:
        print("no fragment volumes found — glob list is broken")
        return 1
    known, sh_text = backup_known_volumes()
    print(f"  fragments define {len(vols)} stateful volumes, "
          f"backup list has {len(known)} entries")

    ok = True
    for name in sorted(set(vols) - known):
        if name in EXCLUDED:
            continue  # allowed absence
        ok = False
        print(f"  ✗ {name} ({', '.join(vols[name])}) is NOT backed up — "
              f"add it to KNOWN_VOLUMES in scripts/backup.sh")
    stale = known - set(vols)
    if stale:
        ok = False
        print(f"  ✗ backup list has entries no fragment defines (stale): "
              f"{sorted(stale)}")
    for name, reason in EXCLUDED.items():
        if name not in sh_text:
            ok = False
            print(f"  ✗ excluded volume {name} ({reason}) must be documented "
                  f"in scripts/backup.sh")
    if "dev-maintenance-bot-state" not in known:
        ok = False
        print("  ✗ the HITL audit trail (dev-maintenance-bot-state) must be "
              "backed up — compliance requires the decision history to "
              "survive host migration")

    if ok:
        print("✅ check_backup: every stateful volume is backed up")
        return 0
    print("❌ check_backup: backup volume list has drifted from the compose "
          "fragments")
    return 1


if __name__ == "__main__":
    sys.exit(main())
