#!/usr/bin/env python3
"""
tests/00-static/check_mail_backend.py — Layer 0 stack-free guard on the mail
backend switch.

The two mail stacks are strictly exclusive (XOR): Stalwart+SOGo (default,
`MAIL_BACKEND=stalwart`) and mailcow (`MAIL_BACKEND=mailcow`, the pinned
submodule driven by scripts/mailcow.sh). Both would fight over `mail.*`
on the Traefik edge and the SMTP/IMAP host ports — so the switch contract
is pinned in the same places the stack boots from:

  1. `.env.example` and `.env.demo` declare `MAIL_BACKEND` (default stalwart,
     mailcow documented as the alternative).
  2. `scripts/mailcow.sh` refuses to boot unless the switch says mailcow,
     and refuses while Stalwart/SOGo containers are running.
  3. The Makefile enforces the same XOR at `make up` time (mailcow switch +
     stalwart overlay in COMPOSE_FILE is an error) and exposes
     mailcow-up/down/purge/status/logs targets.
  4. `README.md` documents the switch and the exclusivity.

Pure file inspection + bash -n, no docker — runs on any fresh checkout,
same as the other Layer 0 checks.

Usage: python3 tests/00-static/check_mail_backend.py
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

ENV_EXAMPLE = ROOT / ".env.example"
ENV_DEMO = ROOT / ".env.demo"
MAILCOW = ROOT / "scripts" / "mailcow.sh"
MAKEFILE = ROOT / "Makefile"
README = ROOT / "README.md"
MAIL_SPEC = ROOT / "specs" / "mail.yml"


def main():
    result = Result("mail-backend-switch")

    failures = []

    # 1. the switch is declared everywhere the stack boots from
    env_ex = ENV_EXAMPLE.read_text()
    if not re.search(r"^MAIL_BACKEND=stalwart\s*$", env_ex, re.M):
        failures.append(".env.example does not declare MAIL_BACKEND=stalwart")
    if "mailcow" not in env_ex:
        failures.append(".env.example does not mention the mailcow alternative")
    if not re.search(r"^MAIL_BACKEND=stalwart\s*$", ENV_DEMO.read_text(), re.M):
        failures.append(".env.demo does not declare MAIL_BACKEND=stalwart")

    # 2. scripts/mailcow.sh enforces the XOR at boot
    sh = MAILCOW.read_text()
    if "backend_switch_ok" not in sh or "MAIL_BACKEND" not in sh:
        failures.append("scripts/mailcow.sh has no MAIL_BACKEND switch guard")
    if re.search(r"opensme-stalwart\|opensme-sogo", sh) is None:
        failures.append(
            "scripts/mailcow.sh does not refuse to boot while "
            "Stalwart/SOGo containers run"
        )
    if not re.search(r"exclusive", sh, re.I):
        failures.append("scripts/mailcow.sh does not state the XOR exclusivity")
    if "backend_switch_ok" not in sh.split("cmd_up")[1].split("cmd_down")[0]:
        failures.append("mailcow.sh up does not call the switch guard")

    # 3. Makefile: XOR guard on `make up` + operator targets
    mk = MAKEFILE.read_text()
    if "MAIL_BACKEND" not in mk:
        failures.append("Makefile has no MAIL_BACKEND awareness")
    if "mail/stalwart.yml" not in mk:
        failures.append("Makefile up-guard does not reference the stalwart overlay")
    for target in ("mailcow-up", "mailcow-down", "mailcow-status"):
        if f"{target}:" not in mk:
            failures.append(f"Makefile lacks the {target} target")

    # 4. README documents the switch
    readme = README.read_text()
    if "MAIL_BACKEND" not in readme:
        failures.append("README does not document MAIL_BACKEND")
    if not re.search(r"stalwart.*mailcow|mailcow.*stalwart", readme, re.I | re.S):
        failures.append("README does not present the two backends together")

    # 5. mail spec flags the switch
    if "MAIL_BACKEND" not in MAIL_SPEC.read_text():
        failures.append("specs/mail.yml does not mention MAIL_BACKEND")

    # 6. scripts/restore.sh passes bash -n (mailcow.sh is part of the boot path)
    for script in (MAILCOW,):
        rc = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True
        ).returncode
        if rc != 0:
            failures.append(f"bash -n fails on {script.name}")

    if failures:
        result.fail("mail backend switch contract broken")
        for f in failures:
            print(f"  ✗ {f}")
    else:
        result.ok(
            "mail backend is switchable (MAIL_BACKEND=stalwart|mailcow, XOR): "
            ".env/.env.demo declare it, mailcow.sh + Makefile enforce it, "
            "README + specs/mail.yml document it"
        )
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
