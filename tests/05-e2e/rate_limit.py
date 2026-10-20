#!/usr/bin/env python3
"""
tests/05-e2e/rate_limit.py — abuse resistance at the edge (Epic AE).

The edge promises rate limiting (traefik/dynamic.yml: average 100 req/s,
burst 200 on both entrypoints). A promise nobody has fired at is
decoration. This journey measures it live:

  AE1  the middleware is declared AND actually mounted into the running
       traefik (dynamic config file inside the container)
  AE2  normal operator traffic never trips it: 100 sequential requests
       (well within the burst bucket) answer 200/3xx, zero 429
  AE3  a flood far beyond the bucket trips it: ≥1 rate-limit response
       appears, the edge stays up (no 5xx), and after a short cool-down
       ordinary traffic flows freely again (bucket refills)

Read-only; the flood is what the middleware exists for.

Usage:
    python3 tests/05-e2e/rate_limit.py [domain]   # default: opensme.local
"""

import argparse
import re
import socket
import subprocess
import sys
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ensure_portal_routed

import requests

T = 10
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
DYNAMIC = Path(__file__).resolve().parent.parent.parent / "traefik" / "dynamic.yml"


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
    ap = argparse.ArgumentParser(description="rate limit journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("rate-limit")
    result.header(f"openSME e2e rate limit journey — domain={args.domain}")

    names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True).stdout.split()
    if not any("opensme-portal" in n for n in names) or \
            not any("opensme-traefik" in n for n in names):
        result.skip("portal/traefik not running (start the stack first)")
        print()
        return 0

    portal = f"https://portal.{args.domain}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False

    if not ensure_portal_routed(session, portal):
        result.fail("portal unreachable via traefik even after resync")
        return 1

    # ── AE1: declared and mounted ───────────────────────────────────
    declared = False
    try:
        text = DYNAMIC.read_text()
        declared = "rateLimit" in text and "average" in text
    except OSError:
        pass
    mounts = subprocess.run(
        ["docker", "inspect", "opensme-traefik",
         "--format", "{{range .Mounts}}{{.Source}} {{end}}"],
        capture_output=True, text=True).stdout
    mounted = "dynamic.yml" in mounts
    (result.ok if declared and mounted else result.fail)(
        "rate-limit middleware declared in traefik/dynamic.yml and mounted "
        "into the running traefik"
        if declared and mounted
        else f"middleware wiring broken: declared={declared} mounted={mounted}"
    )

    # ── AE2: normal traffic never trips it ──────────────────────────
    rate_limited = 0
    server_errors = 0
    for _ in range(100):
        r = session.get(portal + "/health", timeout=T)
        if r.status_code == 429:
            rate_limited += 1
        elif r.status_code >= 500:
            server_errors += 1
    (result.ok if rate_limited == 0 and server_errors == 0 else result.fail)(
        "100 sequential operator requests: zero 429, zero 5xx"
        if rate_limited == 0 and server_errors == 0
        else f"normal traffic punished: 429={rate_limited} 5xx={server_errors}"
    )

    # ── AE3: the flood trips it, the edge survives, the bucket refills ──
    flood = requests.Session()
    flood.verify = False
    flood.trust_env = False
    flood.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=64, pool_maxsize=64))

    def flood_one(_i: int) -> int:
        try:
            return flood.get(portal + "/health", timeout=T).status_code
        except requests.RequestException:
            return 0

    with ThreadPoolExecutor(max_workers=32) as pool:
        codes = list(pool.map(flood_one, range(1000)))

    flood_429 = sum(1 for c in codes if c == 429)
    flood_5xx = sum(1 for c in codes if 500 <= c < 600)
    tripped = flood_429 >= 1 and flood_5xx == 0

    time.sleep(6)  # cool-down: average 100 req/s refills the bucket fast
    after = [session.get(portal + "/health", timeout=T).status_code
             for _ in range(10)]
    refilled = all(c == 200 for c in after)

    (result.ok if tripped and refilled else result.fail)(
        f"flood tripped the limiter ({flood_429}×429, 0×5xx) and the bucket "
        "refilled — ordinary traffic flows freely again"
        if tripped and refilled
        else f"limiter broken: tripped={tripped} (429={flood_429}, "
             f"5xx={flood_5xx}) refilled={refilled} after={after[:5]}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
