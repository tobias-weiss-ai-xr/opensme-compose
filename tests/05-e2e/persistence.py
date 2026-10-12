#!/usr/bin/env python3
"""
tests/05-e2e/persistence.py — state survives restarts (Epic P).

The core promise of self-hosting: your data is still there after the
stack comes back. This journey drives the real lifecycle:

  P1  `up -d` is a no-op on a healthy stack — no container recreation,
      no restart churn (a "deploy" that disrupts nothing)
  P2  data written BEFORE a full stack restart is still there AFTER:
      a marker row lands in a dedicated e2e database, the stack goes
      `down` (volumes kept), comes back `up -d`, and the row survives
  P3  after the round trip the stack is not merely "up" but USEFUL:
      IdP discovery and portal health answer again
  P4  data survives `docker compose up -d --force-recreate --build`:
      marker row persists even when containers are destroyed and rebuilt

The e2e database is created and dropped by the journey itself (idempotent);
the stack's own volumes are never touched.

Usage:
    python3 tests/05-e2e/persistence.py [domain]     # default: opensme.local
"""

import argparse
import os
import os.path
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
PROFILE_ARGS = ["--profile", "standalone"]
DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"
DB = "e2e_persist"
MARKER = "e2e_marker_survives_restart"


def compose_args() -> list[str]:
    cf = os.environ.get("COMPOSE_FILE") or DEMO_SET
    args: list[str] = []
    for f in cf.split(os.pathsep):
        if f:
            args += ["-f", f]
    return args


def compose(*action: str, timeout=300):
    return subprocess.run(
        ["docker", "compose"] + PROFILE_ARGS + compose_args() + list(action),
        capture_output=True, text=True, timeout=timeout)


def psql(sql: str) -> str:
    r = compose("exec", "-T", "postgres",
                "psql", "-U", "opensme", "-d", "postgres", "-Atc", sql,
                timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    return r.stdout.strip()


def started_at(service: str) -> str:
    r = subprocess.run(
        ["docker", "inspect", f"opensme-{service}",
         "--format", "{{.State.StartedAt}}"],
        capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def wait_healthy(session, url, marker=None, deadline_s=180):
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
    ap = argparse.ArgumentParser(description="persistence journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("persistence")
    result.header(f"openSME e2e persistence journey — domain={args.domain}")

    probe = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
    if not any("opensme-postgres" in n for n in probe.stdout.split()):
        result.skip("stack not running")
        print()
        return 0

    session = requests.Session()
    session.verify = False
    session.trust_env = False

    # ── P1: `up -d` on a healthy stack recreates nothing ────────────────
    watch = ("traefik", "postgres", "zitadel", "portal")
    before = {s: started_at(s) for s in watch}
    r = compose("up", "-d")
    if r.returncode != 0:
        result.fail(f"up -d failed: {r.stderr[:150]}")
        return 1
    churned = [s for s in watch if started_at(s) != before[s]]
    (result.ok if not churned else result.fail)(
        "up -d is a no-op: zero recreation churn"
        if not churned else f"up -d disrupted: {'; '.join(churned)} restarted"
    )

    # ── P2: a marker row survives a full stack restart ──────────────────
    try:
        psql(f"DROP DATABASE IF EXISTS {DB}")
        psql(f"CREATE DATABASE {DB}")
        r = compose("exec", "-T", "postgres",
                    "psql", "-U", "opensme", "-d", DB, "-Atc",
                    f"CREATE TABLE {MARKER} (note text); "
                    f"INSERT INTO {MARKER} VALUES ('written-before-restart');",
                    timeout=120)
        if r.returncode != 0:
            result.fail(f"marker write failed: {r.stderr[:120]}")
            return 1
    except (RuntimeError, subprocess.SubprocessError) as e:
        result.fail(f"marker setup failed: {e}")
        return 1

    down = compose("down", "--remove-orphans", timeout=300)
    if down.returncode != 0:
        result.fail(f"stack down failed: {down.stderr[:150]}")
        return 1
    up = compose("up", "-d", timeout=300)
    if up.returncode != 0:
        result.fail(f"stack up failed: {up.stderr[:150]}")
        return 1

    try:
        val = compose("exec", "-T", "postgres", "psql", "-U", "opensme",
                      "-d", DB, "-Atc", f"SELECT note FROM {MARKER}",
                      timeout=120).stdout.strip()
    except (RuntimeError, subprocess.SubprocessError) as e:
        result.fail(f"marker read failed after restart: {e}")
        val = ""
    (result.ok if val == "written-before-restart" else result.fail)(
        "marker row survived the full stack restart (volumes hold state)"
        if val == "written-before-restart"
        else f"data LOST across restart: marker={val!r}"
    )

    # ── P3: the stack is useful again, not just up ──────────────────────
    idp_ok = wait_healthy(
        session, f"https://auth.{args.domain}/.well-known/openid-configuration",
        marker="issuer")
    portal_ok = wait_healthy(session, f"https://portal.{args.domain}/health")
    if idp_ok and portal_ok:
        result.ok("IdP discovery + portal health back after restart")
    else:
        result.fail(f"stack not functional after restart: idp={idp_ok} portal={portal_ok}")

    # ── P4: data survives recreate (force-recreate) ──────────────────
    # Write a second marker, force-recreate containers, verify marker survives
    DB2 = "e2e_persist_p4"
    MARKER2 = "e2e_marker_survives_recreate"
    try:
        psql(f"DROP DATABASE IF EXISTS {DB2}")
        psql(f"CREATE DATABASE {DB2}")
        r = compose("exec", "-T", "postgres",
                    "psql", "-U", "opensme", "-d", DB2, "-Atc",
                    f"CREATE TABLE {MARKER2} (note text); "
                    f"INSERT INTO {MARKER2} VALUES ('p4-before-recreate');",
                    timeout=120)
        if r.returncode != 0:
            result.fail(f"P4 marker write failed: {r.stderr[:120]}")
        else:
            # Force recreate all containers
            recreate = compose("up", "-d", "--force-recreate", "--build", timeout=600)
            if recreate.returncode != 0:
                result.fail(f"P4 force-recreate failed: {recreate.stderr[:150]}")
            else:
                time.sleep(10)  # Allow containers to start
                try:
                    val2 = compose("exec", "-T", "postgres", "psql", "-U", "opensme",
                                  "-d", DB2, "-Atc", f"SELECT note FROM {MARKER2}",
                                  timeout=120).stdout.strip()
                    if val2 == "p4-before-recreate":
                        result.ok("P4 marker survived force-recreate (volumes hold state across --force-recreate)")
                    else:
                        result.fail(f"P4 data LOST across force-recreate: marker={val2!r}")
                except (RuntimeError, subprocess.SubprocessError) as e:
                    result.fail(f"P4 marker read failed after recreate: {e}")
                # Cleanup P4 database
                try:
                    psql(f"DROP DATABASE IF EXISTS {DB2}")
                except (RuntimeError, subprocess.SubprocessError):
                    pass
    except (RuntimeError, subprocess.SubprocessError) as e:
        result.fail(f"P4 setup failed: {e}")

    # cleanup: drop the P1-P3 journey database in every case
    try:
        psql(f"DROP DATABASE IF EXISTS {DB}")
    except (RuntimeError, subprocess.SubprocessError):
        pass

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
