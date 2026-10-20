#!/usr/bin/env python3
"""
tests/05-e2e/burst.py — a small office hits the portal at once (Epic Q).

SME reality: 09:00, everyone opens the workspace. This journey fires a
modest concurrent burst across the portal's surfaces and asserts the
service quality an office can rely on:

  Q1  a burst of parallel requests across landing, health and the
      service catalog returns ZERO 5xx and zero connection errors
  Q2  latency stays within a generous budget (p95) even under the burst
  Q3  the catalog stays CORRECT under load: the same service list as
      the calm single request (no truncation, no mixed responses)

Read-only, self-limiting (40 workers x 8 requests = 320 requests).

Usage:
    python3 tests/05-e2e/burst.py [domain]     # default: opensme.local
"""

import argparse
import re
import socket
import sys
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

import requests

LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
WORKERS = 40
ROUNDS = 8
PATHS = ["/", "/health", "/api/services"]
P95_BUDGET_S = 5.0
TOTAL_BUDGET_S = 90.0


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
    ap = argparse.ArgumentParser(description="burst journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("burst")
    result.header(f"openSME e2e burst journey — domain={args.domain}")

    portal = f"https://portal.{args.domain}"
    try:
        if requests.get(portal + "/health", timeout=10,
                        verify=False).status_code != 200:
            raise requests.RequestException
    except requests.RequestException:
        result.skip("portal not running")
        print()
        return 0

    calm_names = catalog_names(requests.get(portal + "/api/services",
                                             timeout=10, verify=False).text)

    def one(i: int):
        path = PATHS[i % len(PATHS)]
        t0 = time.monotonic()
        try:
            r = requests.get(portal + path, timeout=10, verify=False)
            return path, r.status_code, time.monotonic() - t0, r.text
        except requests.RequestException as e:
            return path, 0, time.monotonic() - t0, f"{e.__class__.__name__}"

    jobs = list(range(WORKERS * ROUNDS))
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        outcomes = list(ex.map(one, jobs))
    wall = time.monotonic() - started

    errors = [o for o in outcomes if o[1] == 0]
    server_errors = [o for o in outcomes if 500 <= o[1] < 600]
    lat = sorted(o[2] for o in outcomes)
    p95 = lat[int(len(lat) * 0.95) - 1] if lat else 0.0

    (result.ok if not errors and not server_errors else result.fail)(
        f"burst {len(outcomes)} requests across {PATHS}: zero errors"
        if not errors and not server_errors
        else f"burst broke: {len(errors)} conn errors, "
             f"{len(server_errors)} 5xx"
    )
    (result.ok if p95 <= P95_BUDGET_S and wall <= TOTAL_BUDGET_S else result.fail)(
        f"latency under burst: p95={p95:.2f}s, wall={wall:.1f}s "
        f"({len(outcomes) / wall:.0f} req/s)"
        if p95 <= P95_BUDGET_S and wall <= TOTAL_BUDGET_S
        else f"latency budget blown: p95={p95:.2f}s (budget {P95_BUDGET_S}s), "
             f"wall={wall:.1f}s"
    )

    catalogs = [o[3] for o in outcomes if o[0] == "/api/services" and o[1] == 200]
    consistent = bool(catalogs) and all(
        catalog_names(body) == calm_names for body in catalogs)
    (result.ok if consistent else result.fail)(
        f"catalog consistent under load ({len(catalogs)} responses, "
        f"{len(calm_names)} services)"
        if consistent
        else "catalog inconsistent under burst: "
             f"calm={calm_names} "
             f"distinct={sorted({tuple(catalog_names(b)) for b in catalogs})[:3]}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


def json_of(body: str):
    import json
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


def catalog_names(body: str) -> list[str]:
    """The advertised service names — the catalog's identity under load."""
    return sorted(str(s.get("name", ""))
                  for s in json_of(body).get("services", []))


if __name__ == "__main__":
    sys.exit(main())
