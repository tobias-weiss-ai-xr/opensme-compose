#!/usr/bin/env python3
"""
tests/05-e2e/broadcast.py — operator broadcast journey (Epic H).

A REAL operator round-trip, not a read-only check:

  1. the operator publishes announcements via the PORTAL_ANNOUNCEMENTS
     environment variable — including an XSS payload as the red team
  2. the portal container is recreated with the new env (docker compose up -d)
  3. a user visiting the landing page sees the notice — and the payload
     arrives HTML-escaped, never executable
  4. the operator withdraws the notice; the portal returns to silence

Requires a running stack and docker; uses the same COMPOSE_FILE selection
as the stack (falls back to the demo quartet like the e2e runner).

Usage:
    python3 tests/05-e2e/broadcast.py [domain]     # default: opensme.local
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
XSS = "<script>alert(1)</script>"
ANNOUNCEMENTS = [
    {"level": "info", "text": "Wartungsfenster: Freitag 18:00"},
    {"level": "warn", "text": XSS},
]
DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"


def compose_args() -> list[str]:
    cf = os.environ.get("COMPOSE_FILE") or DEMO_SET
    args: list[str] = []
    for f in cf.split(os.pathsep):
        if f:
            args += ["-f", f]
    return args


def portal_running() -> bool:
    r = subprocess.run(["docker", "compose"] + compose_args() +
                       ["ps", "--services", "--filter", "status=running"],
                       capture_output=True, text=True, timeout=60)
    return "portal" in r.stdout.split()


def recreate_portal(env_value: str | None) -> None:
    env = dict(os.environ)
    if env_value is None:
        env.pop("PORTAL_ANNOUNCEMENTS", None)  # compose default (empty) applies
    else:
        env["PORTAL_ANNOUNCEMENTS"] = env_value
    subprocess.run(["docker", "compose"] + compose_args() + ["up", "-d", "portal"],
                   env=env, check=True, capture_output=True, timeout=180)


def wait_portal(session, portal_base, deadline_s=90):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            if session.get(portal_base + "/health", timeout=T).status_code == 200:
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
    ap = argparse.ArgumentParser(description="operator broadcast journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("broadcast")
    result.header(f"openSME e2e broadcast journey — domain={args.domain}")

    if not portal_running():
        result.skip("portal not running (start the stack first)")
        print()
        ok = result.summary()
        return 0 if ok else 1

    portal_base = f"https://portal.{args.domain}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False

    payload = json.dumps(ANNOUNCEMENTS)

    # 1) operator publishes (recreate portal with the env override)
    try:
        recreate_portal(payload)
    except subprocess.SubprocessError as e:
        result.fail(f"portal recreate failed: {e.__class__.__name__}")
        return 1
    if not wait_portal(session, portal_base):
        result.fail("portal did not come back healthy after publish")
        recreate_portal(None)
        return 1

    # 2) user sees the notice; the XSS payload arrives escaped
    page = session.get(portal_base + "/", timeout=T)
    api = session.get(portal_base + "/api/announcements", timeout=T)
    ok_api = False
    if api.status_code == 200:
        items = api.json().get("announcements", [])
        ok_api = (len(items) == 2
                  and items[0].get("level") == "info"
                  and items[1].get("level") == "warn")
    visible = "Wartungsfenster: Freitag 18:00" in page.text
    escaped = XSS not in page.text and "&lt;script&gt;" in page.text
    if ok_api and visible and escaped:
        result.ok("broadcast published: api ok, notice visible, XSS escaped")
    else:
        result.fail(f"broadcast broken: api={ok_api} visible={visible} escaped={escaped}")

    # 3) operator withdraws (recreate with the compose default = empty)
    recreate_portal(None)
    if not wait_portal(session, portal_base):
        result.fail("portal did not come back healthy after withdraw")
        return 1
    items = session.get(portal_base + "/api/announcements", timeout=T).json() \
        .get("announcements", [])
    (result.ok if not items else result.fail)(
        "withdrawn: portal is silent again" if not items
        else f"announcement still visible after withdraw: {items!r}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
