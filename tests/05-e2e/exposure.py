#!/usr/bin/env python3
"""
tests/05-e2e/exposure.py — management planes stay closed (Epic M).

A self-hosted stack that leaks its internals is a liability. This journey
inspects the RUNNING stack from an outsider's position:

  M1  only documented ports are published on the host (80, 443, portal 8080)
  M2  stateful services (postgres, redis, memcached) have no host binding —
      and their ports actively refuse connections from outside docker
  M3  traefik's management API/dashboard is not routed on the public entrypoint

Requires docker and a running stack; read-only, no mutations.

Usage:
    python3 tests/05-e2e/exposure.py [domain]     # default: opensme.local
"""

import argparse
import re
import socket
import subprocess
import sys
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

import requests

T = 10
BASE_HOST_PORTS = {"80", "443", "8080"}
# mailcow (optional submodule stack) binds the mail server's ports on the
# host — that is its contract. Allowed only while it is actually running.
MAILCOW_PORTS = {"25", "110", "143", "465", "587", "993", "995", "4190"}
FORBIDDEN_SERVICES = ("postgres", "redis", "memcached")
MANAGEMENT_PATHS = ("/api/http/routers", "/dashboard/", "/api/overview")
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")


def compose_args() -> list[str]:
    cf = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"
    if Path("docker-compose.yml").exists():
        args: list[str] = []
        for f in cf.split(":"):
            args += ["-f", f]
        return args
    return []


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
    ap = argparse.ArgumentParser(description="host exposure journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("exposure")
    result.header(f"openSME e2e exposure journey — domain={args.domain}")

    probe = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
    if not any("opensme" in n for n in probe.stdout.split()):
        result.skip("stack not running")
        print()
        return 0

    # M1: host-published ports — exactly the documented surface
    r = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\\t{{.Ports}}"],
        capture_output=True, text=True)
    allowed = set(BASE_HOST_PORTS)
    if any("mailcow" in n for n in probe.stdout.split()):
        allowed |= MAILCOW_PORTS
    offenders: list[str] = []
    seen: set[str] = set()
    for line in r.stdout.splitlines():
        name, _, ports = line.partition("\t")
        if "opensme" not in name and "mailcow" not in name and "postfix" not in name \
                and "dovecot" not in name:
            continue
        for m in re.finditer(r"(?:0\.0\.0\.0|\[::\]):(\d+)->", ports):
            # only PUBLIC bindings count — loopback-only binds (mailcow's
            # redis/mariadb/doveadm) are safe by design
            hp = m.group(1)
            seen.add(hp)
            if hp not in allowed:
                offenders.append(f"{name}:{hp}")
    if offenders:
        result.fail(f"undocumented host ports published: {', '.join(offenders)}")
    elif seen:
        result.ok(f"host surface documented ({sorted(seen, key=int)} "
                  f"— core: {sorted(BASE_HOST_PORTS, key=int)}"
                  + (", mail: mailcow" if allowed - BASE_HOST_PORTS & seen else "") + ")")
    else:
        result.warn("no published ports found — is the stack up?")

    # M2: stateful services must not answer on the host
    service_ports = {"postgres": "5432", "redis": "6379", "memcached": "11211"}
    for svc, port in service_ports.items():
        bound = any(f"{name.split('opensme-')[-1]}" == svc for name in r.stdout.splitlines()
                    if "opensme" in name and f":{port}->" in name)
        if bound:
            result.fail(f"{svc} publishes {port} on the host!")
            continue
        # live probe from outside docker
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
                result.fail(f"something answers on host port {port} ({svc}?)")
        except OSError:
            result.ok(f"{svc} unreachable from the host (port {port} closed)")

    # M3: traefik management API is not routed on the public entrypoint
    session = requests.Session()
    session.verify = False
    session.trust_env = False
    leaked = []
    for path in MANAGEMENT_PATHS:
        for host in (f"traefik.{args.domain}", args.domain):
            try:
                rr = session.get(f"https://{host}{path}", timeout=T)
                if rr.status_code == 200:
                    leaked.append(f"{host}{path} (HTTP {rr.status_code})")
            except requests.RequestException:
                pass
    (result.ok if not leaked else result.fail)(
        "traefik management API not routed publicly" if not leaked
        else f"management API exposed: {'; '.join(leaked)}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
