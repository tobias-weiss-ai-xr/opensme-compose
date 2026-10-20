#!/usr/bin/env python3
"""
tests/05-e2e/idempotency.py — the second boot is a no-op (Epic X).

Bootstrap code that only works once isn't bootstrap — it's a coin flip.
This journey runs scripts/demo.sh a SECOND time against a live stack
and proves convergence instead of divergence:

  X1  the existing .env survives byte-identical (no silent re-rotation;
      the "using existing .env" path is taken)
  X2  an OIDC app created BEFORE the second boot still exists EXACTLY
      ONCE after it (zitadel setup steps don't duplicate, volumes hold)
  X3  the seeded automation PAT (refreshed by the second boot) still
      authenticates against the Management/Auth API
  X4  portal, IdP and cloud answer 200 afterwards

Cost: one extra demo.sh run (build cache warm, no image changes →
no recreates). CI runs this before persistence, which restarts the
stack anyway.

Usage:
    python3 tests/05-e2e/idempotency.py [domain]   # default: opensme.local
"""

import argparse
import hashlib
import re
import socket
import subprocess
import sys
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import Result, ensure_portal_routed, print_boot_failure

import importlib.util


def _load_suite():
    """Load tests/05-e2e/run.py explicitly — a plain `import run` would
    resolve to tests/run.py (the static runner), shadowing this one."""
    spec = importlib.util.spec_from_file_location(
        "e2e_suite_run", Path(__file__).resolve().parent / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


suite = _load_suite()  # reuses fetch_seeded / mgmt / bootstrap_app

import requests  # noqa: E402 (must follow the suite bootstrap)

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"


def install_dns_fallback() -> None:
    original = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        try:
            return original(host, *args, **kwargs)
        except socket.gaierror:
            if (isinstance(host, str)
                    and re.fullmatch(r"[a-zA-Z0-9.-]+", host)
                    and any(host.endswith(s) for s in LOCALISH_SUFFIXES)):
                return original("127.0.0.1", *args, **kwargs)
            raise

    socket.getaddrinfo = patched


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count_e2e_apps(session, idp_base, pat, pid=None) -> int:
    """Count e2e-sso apps across all projects (search is POST in v1)."""
    if pid is None:
        r = suite.mgmt(session, idp_base, pat, "POST",
                       "/management/v1/projects/_search", {})
        if r.status_code != 200:
            return -1
        pid = next((p["id"] for p in r.json().get("result", [])
                    if p.get("name") == "e2e-sso"), None)
        if pid is None:
            return 0
    r = suite.mgmt(session, idp_base, pat, "POST",
                   f"/management/v1/projects/{pid}/apps/_search", {})
    if r.status_code != 200:
        return -1
    return sum(1 for a in r.json().get("result", [])
               if a.get("name") == "e2e-sso")


def main() -> int:
    ap = argparse.ArgumentParser(description="idempotency journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("idempotency")
    result.header(f"openSME e2e idempotency journey — domain={args.domain}")

    root = Path(__file__).resolve().parent.parent.parent
    env_path = root / ".env"
    if not env_path.exists():
        result.skip(".env not found (boot the stack with scripts/demo.sh first)")
        print()
        return 0

    probe = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
    names = probe.stdout.split()
    if not any("opensme-portal" in n for n in names):
        result.skip("portal not running (start the stack first)")
        print()
        return 0

    portal = f"https://portal.{args.domain}"
    idp = f"https://auth.{args.domain}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False

    try:
        # routing first (traefik provider heals if an earlier journey
        # lost the router)
        if not ensure_portal_routed(session, portal):
            result.fail("portal unreachable via traefik even after resync")
            return 1

        pat_before = suite.fetch_seeded("/machinekey/pat")
        if not pat_before:
            result.skip("automation PAT not seeded — cannot prove app idempotency")
            print()
            return 0

        # provision an OIDC app we can track across the second boot
        callback = f"{portal}/auth/callback"
        post_logout = f"{portal}/"
        try:
            client_id, pid, app_id = suite.bootstrap_app(
                session, idp, pat_before, callback, post_logout)
        except suite.E2EError as e:
            result.skip(f"could not provision tracking app: {e}")
            print()
            return 0

        before = count_e2e_apps(session, idp, pat_before, pid)
        (result.ok if before == 1 else result.fail)(
            "exactly one tracking app before the second boot"
            if before == 1 else f"pre-boot count wrong: {before} e2e-sso apps"
        )

        env_hash_before = sha256(env_path)

        # ── THE second boot ─────────────────────────────────────────
        boot = subprocess.run(["bash", "scripts/demo.sh"], cwd=str(root),
                              capture_output=True, text=True, timeout=1200)
        x1_ok = boot.returncode == 0 and sha256(env_path) == env_hash_before
        if x1_ok:
            result.ok("second demo.sh run succeeded; .env byte-identical")
        else:
            result.fail(f"second boot diverged: rc={boot.returncode} "
                        f"env_changed={sha256(env_path) != env_hash_before}")
            # The boot output was swallowed (capture_output) — without
            # showing it here, a CI flake like "rc=1 env_changed=False"
            # is undiagnosable from the CI log alone.
            print_boot_failure(boot, "demo.sh")

        if not ensure_portal_routed(session, portal):
            result.fail("portal unreachable after the second boot")
            return 1

        # X3: the setup step re-seeds PATs — the fresh one must work
        pat_after = suite.fetch_seeded("/machinekey/pat") or pat_before
        me = suite.mgmt(session, idp, pat_after, "GET",
                        "/auth/v1/users/me")
        (result.ok if me.status_code == 200 else result.fail)(
            "automation PAT valid after the second boot"
            if me.status_code == 200
            else f"automation PAT rejected after reboot: HTTP {me.status_code}"
        )

        # X2: no duplicate app — same pid, count still exactly 1
        after = count_e2e_apps(session, idp, pat_after, pid)
        (result.ok if after == 1 else result.fail)(
            "tracking app exists exactly once after the second boot"
            if after == 1 else f"post-boot count wrong: {after} e2e-sso apps"
        )

        # X4: the stack is healthy
        checks = {
            "portal": session.get(portal + "/health", timeout=T).status_code,
            "idp": session.get(idp + "/debug/ready", timeout=T).status_code,
            "cloud": session.get(f"https://cloud.{args.domain}/.well-known/"
                                 "webfinger", timeout=T).status_code,
        }
        x4 = checks["portal"] == 200 and checks["idp"] == 200 \
            and checks["cloud"] in (200, 400)
        (result.ok if x4 else result.fail)(
            f"stack healthy after the second boot ({checks})"
            if x4 else f"stack unhealthy after reboot: {checks}"
        )
    finally:
        # cleanup the tracking app like the suite does
        try:
            pat = suite.fetch_seeded("/machinekey/pat")
            if pat:
                r = suite.mgmt(session, idp, pat, "POST",
                               "/management/v1/projects/_search", {})
                pid2 = next((p["id"] for p in r.json().get("result", [])
                             if p.get("name") == "e2e-sso"), None)
                if pid2:
                    r = suite.mgmt(session, idp, pat, "POST",
                                   f"/management/v1/projects/{pid2}/apps/_search", {})
                    for a in r.json().get("result", []):
                        if a.get("name") == "e2e-sso":
                            suite.mgmt(session, idp, pat, "DELETE",
                                       f"/management/v1/projects/{pid2}/apps/{a['id']}")
        except Exception:
            pass

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
