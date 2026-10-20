#!/usr/bin/env python3
"""
tests/05-e2e/secret_surface.py — least privilege on secrets (Epic AF).

Every env var a container can read is a breach radius. This journey
walks the RUNNING containers' real environment (docker inspect — what
they can actually see, not what compose declares) and pins four
invariants that hold no matter what services come and go:

  AF1  the edge (traefik) carries NO secret-shaped env vars — the most
       exposed surface holds nothing worth stealing (the dashboard
       basic-auth lives as an apr1 HASH in a label, checked separately)
  AF2  the portal never sees the database password — it talks HTTP to
       the world, not SQL to postgres
  AF3  the zitadel machinekey volume is mounted by exactly one
       container — zitadel itself
  AF4  no container mounts the repo's .env file — host config must not
       become a wholesale container secret dump
  AF5  the traefik dashboard credentials are stored hashed (apr1/
       bcrypt shape), never as plaintext

Generic by design: a NEW container that starts consuming the DB
password or mounting .env trips this journey without any list upkeep.

Usage:
    python3 tests/05-e2e/secret_surface.py [domain]  # default: opensme.local
"""

import argparse
import json
import re
import socket
import subprocess
import sys
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ensure_portal_routed

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
SECRET_SHAPE = re.compile(
    r"(PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|API_KEY|PRIVATE|_PAT$|"
    r"MACHINEKEY|DSN)", re.IGNORECASE)
HASH_SHAPES = ("$apr1$", "$2y$", "$2b$", "$2a$", "$6$", "$5$")


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


def inspect_all() -> dict[str, dict]:
    """name -> {env: [...], mounts: [...], labels: {...}} for the demo
    containers (opensme-*)."""
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True).stdout.split()
    containers = {}
    for name in out:
        if not name.startswith("opensme-"):
            continue
        raw = subprocess.run(
            ["docker", "inspect", name,
             "--format", ("{{json .Config.Env}}|{{json .Mounts}}"
                         "|{{json .Config.Labels}}")],
            capture_output=True, text=True).stdout
        env_json, mounts_json, labels_json = raw.split("|", 2)
        env = [e.split("=", 1)[0] for e in json.loads(env_json)]
        mounts = [m.get("Source") or m.get("Name") or ""
                  for m in json.loads(mounts_json)]
        containers[name] = {"env": env, "mounts": mounts,
                            "labels": json.loads(labels_json)}
    return containers


def main() -> int:
    ap = argparse.ArgumentParser(description="secret surface journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("secret-surface")
    result.header(f"openSME e2e secret surface journey — domain={args.domain}")

    containers = inspect_all()
    if not any("portal" in n for n in containers) or \
            not any("traefik" in n for n in containers):
        result.skip("demo stack not running (start it first)")
        print()
        return 0

    portal = f"https://portal.{args.domain}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False
    if not ensure_portal_routed(session, portal):
        result.fail("portal unreachable via traefik even after resync")
        return 1

    def find(suffix: str) -> str | None:
        hits = [n for n in containers if n.endswith(suffix)]
        return hits[0] if len(hits) == 1 else None

    # ── AF1: the edge carries no secret-shaped env ──────────────────
    traefik = find("traefik")
    edge = [e for e in containers.get(traefik, {}).get("env", [])
            if SECRET_SHAPE.search(e)]
    (result.ok if traefik and not edge else result.fail)(
        f"traefik env holds no secret-shaped vars ({len(containers.get(traefik, {}).get('env', []))} vars total)"
        if traefik and not edge
        else f"edge carries secrets: {edge[:4]}"
    )

    # ── AF2: the portal never sees the database password ────────────
    portal_c = find("portal")
    db_leak = [e for e in containers.get(portal_c, {}).get("env", [])
               if re.search(r"POSTGRES.*(PASSWORD|PASSWD)|DB_PASSWORD",
                            e, re.IGNORECASE)]
    (result.ok if portal_c and not db_leak else result.fail)(
        "portal env holds no database credentials"
        if portal_c and not db_leak
        else f"portal sees DB secrets: {db_leak[:3]}"
    )

    # ── AF3: machinekey mounted by exactly one container ────────────
    mk_holders = [n for n, c in containers.items()
                  if any("machinekey" in (m or "").lower()
                         for m in c["mounts"])]
    single = len(mk_holders) == 1 and "zitadel" in mk_holders[0]
    (result.ok if single else result.fail)(
        f"zitadel machinekey mounted by exactly one container: {mk_holders}"
        if single
        else f"machinekey surface too wide: {mk_holders}"
    )

    # ── AF4: no container mounts the repo's .env ────────────────────
    env_mounts = [n for n, c in containers.items()
                  if any(re.search(r"/\.env$|/\.env\b", (m or ""))
                         for m in c["mounts"])]
    (result.ok if not env_mounts else result.fail)(
        ".env is mounted into no container"
        if not env_mounts
        else f".env leaked into containers: {env_mounts}"
    )

    # ── AF5: dashboard credentials are hashed ───────────────────────
    users = (containers.get(traefik, {}).get("labels", {})
             .get("traefik.http.middlewares.auth.basicauth.users", ""))
    hashed = bool(users) and users.count(":") >= 1 and \
        any(users.split(":", 1)[1].startswith(h) for h in HASH_SHAPES)
    (result.ok if hashed else result.fail)(
        "dashboard basic-auth stored as a hash, not plaintext"
        if hashed
        else "dashboard credentials not hashed (or missing)"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
