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
        return 0 if result.summary() else 1

    files = sorted(BPMN_DIR.glob("*.bpmn"))
    if not files:
        result.fail("bootstrap/bpmn/ has no .bpmn files")
    for f in files:
        check_file(f, result)
    check_deploy_script(result)
    check_env_generator(result)
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
    for key in ("company", "domain", "users", "groups"):
        if key not in data:
            result.fail(f"bootstrap/demo-users.json missing '{key}' key")
    result.ok("bootstrap/demo-users.json has all required keys")
    
    users = data.get("users", [])
    if not isinstance(users, list) or len(users) < 12:
        result.fail(f"bootstrap/demo-users.json needs at least 12 users, has {len(users) if isinstance(users, list) else 'not a list'}")
    else:
        result.ok(f"bootstrap/demo-users.json: {len(users)} demo users configured")
    
    # Each user needs required fields
    required_user_fields = {"username", "firstName", "lastName", "displayName", "email", "password"}
    for i, user in enumerate(users):
        uname = user.get("username", "unknown")
        missing = required_user_fields - set(user.keys())
        if missing:
            result.fail(f"bootstrap/demo-users.json user[{i}] ({uname}): missing {missing}")
        else:
            result.ok(f"bootstrap/demo-users.json user[{i}] ({uname}): all required fields")
    
    if not DEMO_SCRIPT.is_file():
        result.fail("scripts/provision-demo-users.sh missing")
    elif not DEMO_SCRIPT.stat().st_mode & 0o111:
        result.fail("scripts/provision-demo-users.sh is not executable")
    else:
        result.ok("scripts/provision-demo-users.sh present and executable")


if __name__ == "__main__":
    sys.exit(main())
