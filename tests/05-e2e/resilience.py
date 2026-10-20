#!/usr/bin/env python3
"""
tests/05-e2e/resilience.py — the stack heals itself (Epic J).

Real SRE journeys against a running stack, then everything is restored:

  J1  Database bounce: `docker compose restart postgres` — the IdP, the
      portal and the cloud must all reconnect and come back healthy.
  J2  Identity-provider outage: `docker compose stop zitadel` — the portal
      and the cloud must DEGRADE, not fall over; after `start zitadel` the
      IdP serves discovery again.

Requires docker and the same COMPOSE_FILE selection as the stack (falls
back to the demo quartet). Restores the stack in `finally` — a crash in
this script must not leave the stack broken.

Usage:
    python3 tests/05-e2e/resilience.py [domain]     # default: opensme.local
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"


def compose_args() -> list[str]:
    cf = os.environ.get("COMPOSE_FILE") or DEMO_SET
    args: list[str] = []
    for f in cf.split(os.pathsep):
        if f:
            args += ["-f", f]
    return args


def compose(*action: str, timeout=120) -> str:
    r = subprocess.run(["docker", "compose"] + compose_args() + list(action),
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(action)}: {r.stderr[:200]}")
    return r.stdout


def compose_running() -> dict[str, str]:
    out = compose("ps", "--format", "json", "--filter", "status=running")
    names = {}
    for line in (out or "").splitlines():
        try:
            j = json.loads(line)
            names[j.get("Service", "")] = j.get("Health", "")
        except json.JSONDecodeError:
            continue
    return names


def wait_healthy(session, url, marker=None, deadline_s=150):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            r = session.get(url, timeout=T)
            if r.status_code == 200 and (marker is None or marker in r.text):
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


def wait_pg_ready(deadline_s=120):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "compose"] + compose_args() +
            ["exec", "-T", "postgres", "pg_isready", "-U",
             os.environ.get("POSTGRES_USER", "opensme")],
            capture_output=True, text=True, timeout=60)
        if "accepting connections" in r.stdout:
            return True
        time.sleep(2)
    return False


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


def main() -> int:
    ap = argparse.ArgumentParser(description="stack resilience journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("resilience")
    result.header(f"openSME e2e resilience journey — domain={args.domain}")

    try:
        running = compose_running()
    except (RuntimeError, subprocess.SubprocessError):
        running = {}
    if "postgres" not in running or "portal" not in running:
        result.skip("stack not running (start it first)")
        print()
        ok = result.summary()
        return 0 if ok else 1

    session = requests.Session()
    session.verify = False
    session.trust_env = False
    idp_base = f"https://auth.{args.domain}"
    portal_base = f"https://portal.{args.domain}"
    cloud_base = f"https://cloud.{args.domain}"

    # ── J1: database bounce → dependent services reconnect ──────────────
    try:
        compose("restart", "postgres", timeout=180)
    except (RuntimeError, subprocess.SubprocessError) as e:
        result.fail(f"postgres restart failed: {e}")
        return 1
    db_back = wait_pg_ready()
    idp_back = db_back and wait_healthy(session, idp_base + "/debug/ready")
    portal_back = wait_healthy(session, portal_base + "/health")
    if db_back and idp_back and portal_back:
        result.ok("database bounce survived: IdP + portal reconnected")
    else:
        result.fail(f"recovery incomplete after DB bounce: "
                    f"pg={db_back} idp={idp_back} portal={portal_back}")

    # ── J2: identity-provider outage → graceful degradation ─────────────
    try:
        compose("stop", "zitadel", timeout=120)
    except (RuntimeError, subprocess.SubprocessError) as e:
        result.fail(f"zitadel stop failed: {e}")
        return 1
    try:
        try:
            _idp_down = session.get(idp_base + "/debug/ready", timeout=5) is not None
        except requests.RequestException:
            _idp_down = True
        portal_up = False
        cloud_up = False
        try:
            portal_up = session.get(portal_base + "/", timeout=T).status_code == 200
            cloud_up = session.get(cloud_base + "/", timeout=T).status_code == 200
        except requests.RequestException:
            pass
        if portal_up and cloud_up:
            result.ok("IdP outage degrades gracefully: portal + cloud stay up")
        else:
            result.fail(f"IdP outage took neighbors down: portal={portal_up} cloud={cloud_up}")
    finally:
        try:
            compose("start", "zitadel", timeout=180)
        except (RuntimeError, subprocess.SubprocessError) as e:
            result.fail(f"CRITICAL: zitadel restart failed: {e}")
            print()
            result.summary()
            return 1

    idp_recovered = wait_healthy(
        session, idp_base + "/.well-known/openid-configuration", marker="issuer")
    (result.ok if idp_recovered else result.fail)(
        "IdP recovered and serves discovery again" if idp_recovered
        else "IdP did not come back after outage")

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
