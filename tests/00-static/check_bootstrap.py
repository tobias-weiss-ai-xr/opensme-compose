#!/usr/bin/env python3
"""
tests/00-static/check_bootstrap.py — Layer 0 lint for seed/bootstrap data.

The portal "Workflow" card and the Operaton engine advertise seed BPMN
processes from bootstrap/bpmn/. Broken seed data must fail in CI, not at
demo time — Operaton rejects a deployment (HTTP 400) when:

  * the XML is not well-formed,
  * IDs collide within a deployment (cvc-id.2),
  * the <process> element carries no operaton:historyTimeToLive
    (ENGINE-12018 — "History Time To Live (TTL) cannot be null"),
  * a serviceTask uses a bare boolean operaton:expression (ClassCastException
    at instance start: Boolean cannot be cast to String),
  * a <conditionExpression> omits the xsi namespace declaration.

Usage: python3 tests/00-static/check_bootstrap.py
"""

import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT  # noqa: E402

BPMN_DIR = ROOT / "bootstrap" / "bpmn"
DEPLOY_SCRIPT = ROOT / "bootstrap" / "bpmn-deploy.sh"
ENV_GEN_SCRIPT = ROOT / "scripts" / "init-env.sh"
OPERATON_NS = "http://operaton.org/schema/1.0/bpmn"

SEED_DIR = ROOT / "bootstrap" / "seed-content"
SEED_README = SEED_DIR / "README.md"
# The content documents advertised in seed-content/README.md (name -> purpose).
SEED_DOCUMENTS = {
    "01-welcome.md": "orientation note mapping services -> what they do",
    "02-team-charter.md": "a 'how we work' agreement members can adopt",
    "03-decision-record.md": "RFC-style record that mirrors the approval workflow",
    "04-onboarding-checklist.md": "repeatable new-member checklist",
}
# Editorial rule from seed-content/README.md: keep each document under ~40 lines.
SEED_MAX_LINES = 40
# Contents ship inside the repo — they must never carry credentials.
SECRET_MARKERS = (
    "BEGIN RSA PRIVATE KEY",
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN EC PRIVATE KEY",
    "BEGIN PRIVATE KEY",
    "AKIA",
)


def local(tag: str) -> str:
    """Strip the {namespace} prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1]


def check_file(path: Path, result: Result) -> None:
    name = path.name
    raw = path.read_text()
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        result.fail(f"{name}: XML is not well-formed: {e}")
        return
    result.ok(f"{name}: XML well-formed")

    # ── unique IDs across the whole document ──────────────────────
    ids = [e.get("id") for e in root.iter() if e.get("id")]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        result.fail(f"{name}: duplicate id(s) — Operaton rejects cvc-id.2: {dupes}")
    else:
        result.ok(f"{name}: {len(ids)} unique id(s)")

    # ── every process needs a history TTL + an id distinct from <definitions>
    processes = [e for e in root.iter() if local(e.tag) == "process"]
    if not processes:
        result.fail(f"{name}: no <process> element")
        return
    definition_id = root.get("id")
    for proc in processes:
        pid = proc.get("id")
        ttl = proc.get(f"{{{OPERATON_NS}}}historyTimeToLive")
        if not ttl:
            result.fail(
                f"{name}: process {pid!r} has no operaton:historyTimeToLive "
                "(ENGINE-12018 — deployment would be rejected)"
            )
        else:
            result.ok(f"{name}: process {pid!r} historyTimeToLive={ttl}")
        if pid and pid == definition_id:
            result.fail(f"{name}: process id collides with <definitions> id ({pid!r})")

    # ── serviceTasks must not use a bare boolean expression ───────
    for task in root.iter():
        if local(task.tag) != "serviceTask":
            continue
        expr = task.get(f"{{{OPERATON_NS}}}expression")
        if expr is None:
            continue
        body = expr.strip()
        if body in ("${true}", "${false}"):
            result.fail(
                f"{name}: serviceTask {task.get('id')!r} uses bare boolean {body} "
                "— ClassCastException at start (use execution.setVariable(...))"
            )
        else:
            result.ok(f"{name}: serviceTask {task.get('id')!r} expression ok")

    # ── conditionExpression needs the xsi namespace in scope ──────
    if "conditionExpression" in raw and "xmlns:xsi=" not in raw:
        result.fail(f"{name}: conditionExpression without xmlns:xsi declaration")
    elif "conditionExpression" in raw:
        result.ok(f"{name}: conditionExpression xsi namespace declared")


def check_seed_content(result: Result) -> None:
    """bootstrap/seed-content/ ships sample Markdown documents (see its
    README for the editorial contract): every documented file must be
    present, non-empty, start with an H1, end with a newline, stay under
    the ~40-line budget, and never carry credentials."""
    if not SEED_DIR.is_dir():
        result.fail(f"{SEED_DIR} missing")
        return
    result.ok("bootstrap/seed-content/ present")

    if not SEED_README.is_file():
        result.fail("bootstrap/seed-content/README.md missing")
        return
    result.ok("bootstrap/seed-content/README.md present")

    docs = {p.name: p for p in sorted(SEED_DIR.glob("*.md")) if p != SEED_README}
    missing = sorted(set(SEED_DOCUMENTS) - set(docs))
    if missing:
        result.fail(
            f"bootstrap/seed-content/ missing documented file(s): {missing} "
            "— README.md 'Documents' table advertises them"
        )
    else:
        result.ok(f"bootstrap/seed-content/: all {len(SEED_DOCUMENTS)} documented files present")

    for name, purpose in sorted(SEED_DOCUMENTS.items()):
        path = docs.get(name)
        if path is None:
            continue
        checks = 0
        raw = path.read_text()
        if not raw.strip():
            result.fail(f"{name}: empty document")
        else:
            checks += 1
            result.ok(f"{name}: non-empty")
        if not raw.startswith("# "):
            result.fail(f"{name}: does not start with an H1 heading")
        else:
            checks += 1
            result.ok(f"{name}: starts with H1 ({raw.splitlines()[0][2:].strip()!r})")
        if not raw.endswith("\n"):
            result.fail(f"{name}: missing trailing newline")
        else:
            checks += 1
            result.ok(f"{name}: ends with newline")
        lines = len(raw.splitlines())
        if lines > SEED_MAX_LINES:
            result.fail(f"{name}: {lines} lines > ~{SEED_MAX_LINES} editorial budget "
                        "(seed-content/README.md)")
        else:
            checks += 1
            result.ok(f"{name}: {lines} lines within editorial budget")
        hits = [m for m in SECRET_MARKERS if m in raw]
        if hits:
            result.fail(f"{name}: credential marker(s) in shipped doc: {hits}")
        else:
            checks += 1
            result.ok(f"{name}: no credential markers")
        if checks == 5:
            result.ok(f"{name}: seed document passes ({purpose})")


def check_deploy_script(result: Result) -> None:
    if not DEPLOY_SCRIPT.is_file():
        result.fail("bootstrap/bpmn-deploy.sh missing")
        return
    result.ok("bootstrap/bpmn-deploy.sh present")
    if not DEPLOY_SCRIPT.stat().st_mode & 0o111:
        result.fail("bootstrap/bpmn-deploy.sh is not executable")
    else:
        result.ok("bootstrap/bpmn-deploy.sh executable")


def check_env_generator(result: Result) -> None:
    """scripts/init-env.sh must emit a working .env: every CHANGEME_
    placeholder replaced with a random value, an apr1 Traefik hash, and
    0600 permissions — and must refuse to clobber an existing .env."""
    if not ENV_GEN_SCRIPT.is_file():
        result.fail("scripts/init-env.sh missing")
        return
    result.ok("scripts/init-env.sh present")

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        (tdir / "scripts").mkdir()
        shutil.copy(ENV_GEN_SCRIPT, tdir / "scripts" / "init-env.sh")
        shutil.copy(ROOT / ".env.example", tdir / ".env.example")

        env = {**__import__("os").environ}
        env.pop("ENV_GEN_SKIP", None)
        run = subprocess.run(
            ["bash", "scripts/init-env.sh"], cwd=tdir, env=env,
            capture_output=True, text=True,
        )
        if run.returncode != 0:
            result.fail(f"init-env.sh exit={run.returncode}: {run.stderr.strip()}")
            return
        out = (tdir / ".env").read_text()

        if "=CHANGEME_" in out:
            result.fail("init-env.sh left unresolved CHANGEME_ placeholder(s)")
        else:
            result.ok("init-env.sh replaced all CHANGEME_ placeholders")

        traefik = next(
            (ln for ln in out.splitlines() if ln.startswith("TRAEFIK_USERS=")), ""
        )
        if "$apr1$" not in traefik and "$$apr1$$" not in traefik:
            result.fail(f"init-env.sh TRAEFIK_USERS is not an apr1 hash: {traefik!r}")
        else:
            result.ok("init-env.sh produced apr1 Traefik hash")

        if (tdir / ".env").stat().st_mode & 0o777 != 0o600:
            result.fail("init-env.sh .env is not mode 0600")
        else:
            result.ok("init-env.sh .env locked to 0600")

        # ── idempotency: second run must refuse to clobber ──
        before = (tdir / ".env").read_text()
        run2 = subprocess.run(
            ["bash", "scripts/init-env.sh"], cwd=tdir, env=env,
            capture_output=True, text=True,
        )
        if run2.returncode != 0 or (tdir / ".env").read_text() != before:
            result.fail("init-env.sh is not idempotent (clobbered existing .env)")
        else:
            result.ok("init-env.sh is idempotent (no-clobber)")


def main() -> int:
    result = Result("bootstrap-seed-data")
    if not BPMN_DIR.is_dir():
        result.fail(f"{BPMN_DIR} missing")
    else:
        files = sorted(BPMN_DIR.glob("*.bpmn"))
        if not files:
            result.fail("bootstrap/bpmn/ has no .bpmn files")
        for f in files:
            check_file(f, result)
        check_deploy_script(result)
        check_env_generator(result)
    # seed-content + demo-users checks run regardless of bpmn/ state.
    check_seed_content(result)
    check_demo_users(result)
    return 0 if result.summary() else 1


def check_demo_users(result: Result) -> None:
    """bootstrap/demo-users.json offers 12 pre-configured users for demos."""
    DEMO_USERS = ROOT / "bootstrap" / "demo-users.json"
    DEMO_SCRIPT = ROOT / "scripts" / "provision-demo-users.sh"
    
    if not DEMO_USERS.is_file():
        result.fail("bootstrap/demo-users.json missing")
        return
    result.ok("bootstrap/demo-users.json present")
    
    # Validate JSON structure
    import json
    try:
        data = json.loads(DEMO_USERS.read_text())
    except json.JSONDecodeError as e:
        result.fail(f"bootstrap/demo-users.json: invalid JSON: {e}")
        return
    result.ok("bootstrap/demo-users.json valid JSON")
    
    # Check required top-level keys
    missing_keys = [k for k in ("company", "domain", "users", "groups") if k not in data]
    if missing_keys:
        result.fail(f"bootstrap/demo-users.json missing top-level key(s): {missing_keys}")
    else:
        result.ok("bootstrap/demo-users.json has all required top-level keys")

    users = data.get("users", [])
    if not isinstance(users, list) or len(users) < 12:
        result.fail(
            f"bootstrap/demo-users.json needs at least 12 users, has "
            f"{len(users) if isinstance(users, list) else 'not a list'}"
        )
    else:
        result.ok(f"bootstrap/demo-users.json: {len(users)} demo users configured")

    # Each user needs every field the provisioning script consumes
    # (scripts/provision-demo-users.sh: identity, names, mail, credentials
    # and group membership from roles/department).
    required_user_fields = {
        "id", "username", "firstName", "lastName", "displayName",
        "email", "password", "roles", "department",
    }
    seen_ids, seen_usernames = set(), set()
    for i, user in enumerate(users):
        uname = user.get("username", "unknown")
        missing = required_user_fields - set(user.keys())
        if missing:
            result.fail(f"bootstrap/demo-users.json user[{i}] ({uname}): missing {missing}")
        else:
            result.ok(f"bootstrap/demo-users.json user[{i}] ({uname}): all required fields")
        blank = [k for k in ("username", "firstName", "lastName", "displayName", "email")
                 if not str(user.get(k, "")).strip()]
        if blank:
            result.fail(f"bootstrap/demo-users.json user[{i}] ({uname}): blank field(s) {blank}")
        else:
            result.ok(f"bootstrap/demo-users.json user[{i}] ({uname}): identity fields non-blank")
        if uname in seen_usernames:
            result.fail(f"bootstrap/demo-users.json: duplicate username {uname!r}")
        seen_usernames.add(uname)
        uid = user.get("id")
        if uid in seen_ids:
            result.fail(f"bootstrap/demo-users.json: duplicate user id {uid!r}")
        seen_ids.add(uid)
        email = user.get("email", "")
        if email and not email.endswith(f"@{data.get('domain', '')}"):
            result.fail(
                f"bootstrap/demo-users.json user {uname!r}: email {email!r} not under "
                f"domain @{data.get('domain', '')} (provisioning assumes it)"
            )
        if not user.get("password"):
            result.fail(f"bootstrap/demo-users.json user {uname!r}: empty password")
        roles = user.get("roles")
        if not isinstance(roles, list) or not roles:
            result.fail(f"bootstrap/demo-users.json user {uname!r}: roles must be a non-empty list")
        if not user.get("department"):
            result.fail(f"bootstrap/demo-users.json user {uname!r}: empty department")

    if len(users) == 12 and len(seen_usernames) == 12 and len(seen_ids) == 12:
        result.ok("bootstrap/demo-users.json: 12 unique users with unique ids/usernames")
    
    if not DEMO_SCRIPT.is_file():
        result.fail("scripts/provision-demo-users.sh missing")
    elif not DEMO_SCRIPT.stat().st_mode & 0o111:
        result.fail("scripts/provision-demo-users.sh is not executable")
    else:
        result.ok("scripts/provision-demo-users.sh present and executable")


if __name__ == "__main__":
    sys.exit(main())
