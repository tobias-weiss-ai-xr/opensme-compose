#!/usr/bin/env python3
"""
tests/04-integration/run.py — cross-service integration contracts.

Unlike Layer 3 (single-service reachability from the host), this layer
verifies the WIRING between services on a running stack:

  1. databases     postgres provisions a database for every DB-backed
                   service that is running (spec: postgres-init/*.sql)
  2. pgbouncer     answers the PostgreSQL protocol inside the network
                   (probed from postgres via pg_isready — DNS + pool)
  3. sso-issuer    opencloud's OC_OIDC_ISSUER matches the issuer zitadel
                   actually serves via discovery (SSO wiring consistency)
  4. ai-proxy      portal → llama.cpp round-trip through the real AI
                   proxy endpoint (the live twin of the in-process
                   contract tests in portal/src/main.rs)

Each check SKIPS (not fails) when a participating service isn't running,
so the layer is meaningful for any COMPOSE_FILE subset.

Usage:
    python3 tests/04-integration/run.py [--domain opensme.local]

Run against the same COMPOSE_FILE selection as the stack:
    COMPOSE_FILE="docker-compose.yml:idm/zitadel.yml:..." \
        python3 tests/04-integration/run.py
"""

import argparse
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

import requests

T = 20
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")

# service name (compose) → expected database (postgres-init/00-create-databases.sql)
SERVICE_DB = {
    "zitadel": "zitadel",
    "casdoor": "casdoor_db",
    "sogo": "sogo",
    "synapse": "synapse_db",
    "paperless-ngx": "paperless_db",
    "invoiceninja": "invoiceninja_db",
    "notes-backend": "notes_db",
    "nosdesk": "nosdesk_db",
}


def load_env() -> dict:
    """Minimal .env parser (KEY=VALUE lines, no interpolation)."""
    env = {}
    path = Path(__file__).resolve().parent.parent / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def compose_exec(service: str, cmd: list[str], timeout: int = T):
    return subprocess.run(
        ["docker", "compose", "exec", "-T", service] + cmd,
        capture_output=True, text=True, timeout=timeout,
    )


def running_services() -> set[str]:
    r = subprocess.run(
        ["docker", "compose", "ps", "--services", "--filter", "status=running"],
        capture_output=True, text=True, timeout=30,
    )
    return set(r.stdout.split())


def install_dns_fallback() -> None:
    """Same trick as the e2e runner: *.local/.localhost/.test that fail real
    DNS resolve to loopback (local Traefik). Public domains stay untouched."""
    original = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        try:
            return original(host, *args, **kwargs)
        except socket.gaierror:
            if (
                isinstance(host, str)
                and re.fullmatch(r"[a-zA-Z0-9.-]+", host)
                and any(host.endswith(s) for s in LOCALISH_SUFFIXES)
            ):
                return original("127.0.0.1", *args, **kwargs)
            raise

    socket.getaddrinfo = patched


def edge_base(domain: str) -> str:
    return f"https://portal.{domain}"


def session_insecure() -> requests.Session:
    s = requests.Session()
    s.verify = False  # local demo stacks use self-signed certs
    s.trust_env = False  # never route through a host proxy
    return s


# ── 1. database provisioning per running service ──────────────────────
def check_databases(result: Result, running: set[str], env: dict):
    if "postgres" not in running:
        result.skip("postgres not running")
        return
    user = env.get("POSTGRES_USER", "opensme")
    r = compose_exec("postgres", ["psql", "-U", user, "-tAc",
                                  "SELECT datname FROM pg_database"])
    if r.returncode != 0:
        result.fail(f"psql probe failed: {r.stderr.strip()[:100]}")
        return
    dbs = set(r.stdout.split())
    missing = [f"{svc}→{db}" for svc, db in SERVICE_DB.items()
               if svc in running and db not in dbs]
    if missing:
        result.fail(f"missing databases for running services: {', '.join(missing)}")
    else:
        n = sum(1 for svc in SERVICE_DB if svc in running)
        result.ok(f"database provisioned for all {n} DB-backed service(s)")


# ── 2. pgbouncer speaks the pg protocol inside the network ─────────────
def check_pgbouncer(result: Result, running: set[str]):
    if "postgres" not in running:
        result.skip("postgres not running")
        return
    if "pgbouncer" not in running:
        result.skip("pgbouncer not running (direct-PG profile)")
        return
    r = compose_exec("postgres", ["pg_isready", "-h", "pgbouncer", "-p", "6432"])
    if r.returncode == 0 and "accepting connections" in r.stdout:
        result.ok("pgbouncer answers pg protocol inside the network")
    else:
        result.fail(f"pgbouncer unreachable via pg protocol: "
                    f"{(r.stdout + r.stderr).strip()[:100]}")


# ── 3. SSO issuer consistency (opencloud ↔ zitadel via discovery) ─────
def check_sso_issuer(result: Result, running: set[str], domain: str):
    for svc in ("opencloud", "zitadel"):
        if svc not in running:
            result.skip(f"{svc} not running")
            return
    r = compose_exec("opencloud", ["sh", "-c", "env | grep -E '^OC_OIDC_ISSUER='"])
    if r.returncode != 0 or "=" not in r.stdout:
        result.skip("OC_OIDC_ISSUER not visible in opencloud env")
        return
    oc_issuer = r.stdout.strip().split("=", 1)[1].rstrip("/")

    try:
        disc = session_insecure().get(
            f"https://auth.{domain}/.well-known/openid-configuration",
            timeout=T,
        ).json()
    except (requests.RequestException, ValueError) as e:
        result.fail(f"OIDC discovery via edge failed: {e}")
        return
    served = str(disc.get("issuer", "")).rstrip("/")
    if not served:
        result.fail("discovery document has no issuer")
    elif served == oc_issuer:
        result.ok(f"opencloud issuer matches zitadel discovery ({served})")
    else:
        result.fail(f"issuer mismatch: opencloud={oc_issuer!r} zitadel={served!r}")


# ── 4. live AI proxy round-trip (portal → llama.cpp) ───────────────────
def check_ai_proxy(result: Result, running: set[str], domain: str, env: dict):
    if "ai" not in running or "portal" not in running:
        result.skip("ai/portal not running (--profile ai enables this check)")
        return
    s = session_insecure()
    headers = {}
    key = env.get("AI_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    last = ""
    for attempt in range(5):  # model load can take a while on first boot
        try:
            r = s.post(edge_base(domain) + "/api/ai/chat",
                       json={"question": "ping"}, headers=headers, timeout=120)
        except requests.RequestException as e:
            result.fail(f"AI proxy round-trip failed: {e}")
            return
        if r.status_code == 200:
            answer = r.json().get("answer", "")
            if answer:
                result.ok(f"AI proxy round-trip ok (answer: {answer[:40]!r})")
                return
            last = "empty answer"
        else:
            last = f"HTTP {r.status_code} {r.text[:80]}"
        time.sleep(5)
    result.fail(f"AI proxy round-trip did not succeed ({last})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default=None,
                        help="base domain (default: OPENSME_DOMAIN from .env "
                             "or opensme.local)")
    args = parser.parse_args()

    env = load_env()
    domain = args.domain or env.get("OPENSME_DOMAIN", "opensme.local")
    install_dns_fallback()
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("integration")
    running = running_services()
    if not running:
        result.skip("no running stack (docker compose ps is empty)")
        print()
        ok = result.summary()
        sys.exit(0 if ok else 1)

    result.info("Check 1: postgres provisions databases for running services")
    check_databases(result, running, env)
    result.info("Check 2: pgbouncer pg protocol inside the network")
    check_pgbouncer(result, running)
    result.info("Check 3: SSO issuer consistency (opencloud ↔ zitadel)")
    check_sso_issuer(result, running, domain)
    result.info("Check 4: live AI proxy round-trip (portal → llama.cpp)")
    check_ai_proxy(result, running, domain, env)

    print()
    ok = result.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
