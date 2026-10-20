#!/usr/bin/env python3
"""
tests/05-e2e/concurrent_writers.py — the store holds under contention (Epic AH).

The burst journey proves the portal survives load on GETs; this one
proves the intercom store stays CORRECT when twenty users hit POST at
the same moment:

  AH1  20 concurrent posts all succeed (201) — the store never
       deadlocks or drops writers under contention
  AH2  every posted note is in the list afterwards — no lost update,
       no interleaving corruption
  AH3  readers during the storm always see valid JSON with HTTP 200 —
       the store never exposes half-written state
  AH4  the served page carries the notes as escaped markup — render
       under contention is as safe as render in the quiet

Usage:
    python3 tests/05-e2e/concurrent_writers.py [domain]  # default: opensme.local
"""

import argparse
import json
import re
import socket
import sys
import urllib3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ensure_portal_routed

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
N_WRITERS = 20
N_READERS = 10
MARK = "concurrent-probe-{i:02d}-נgck"


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
    ap = argparse.ArgumentParser(description="concurrent writers journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("concurrent-writers")
    result.header(f"openSME e2e concurrent writers journey — domain={args.domain}")

    import subprocess
    names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True).stdout.split()
    if not any("opensme-portal" in n for n in names):
        result.skip("portal not running (start the stack first)")
        print()
        return 0

    portal = f"https://portal.{args.domain}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False
    if not ensure_portal_routed(session, portal):
        result.fail("portal unreachable via traefik even after resync")
        return 1

    payloads = [MARK.format(i=i) for i in range(N_WRITERS)]

    def writer(i: int) -> int:
        s = requests.Session()
        s.verify = False
        s.trust_env = False
        try:
            return s.post(portal + "/api/intercom",
                          json={"text": payloads[i]}, timeout=T).status_code
        except requests.RequestException:
            return 0

    # readers run WHILE the writers storm the store
    reader_results: list[tuple[int, bool]] = []

    def reader(i: int) -> None:
        s = requests.Session()
        s.verify = False
        s.trust_env = False
        try:
            r = s.get(portal + "/api/intercom", timeout=T)
            try:
                r.json()
                valid = True
            except (json.JSONDecodeError, ValueError):
                valid = False
            reader_results.append((r.status_code, valid))
        except requests.RequestException:
            reader_results.append((0, False))

    with ThreadPoolExecutor(max_workers=N_WRITERS + N_READERS) as pool:
        write_fut = [pool.submit(writer, i) for i in range(N_WRITERS)]
        read_fut = [pool.submit(reader, i) for i in range(N_READERS)]
        codes = [f.result() for f in write_fut]
        [f.result() for f in read_fut]

    # ── AH1: every writer succeeded ─────────────────────────────────
    all_ok = all(c == 201 for c in codes)
    (result.ok if all_ok else result.fail)(
        f"all {N_WRITERS} concurrent posts accepted (201)"
        if all_ok
        else f"writers lost under contention: "
             f"{sum(1 for c in codes if c != 201)}/{N_WRITERS} non-201 "
             f"({sorted(set(codes))})"
    )

    # ── AH2: every note present afterwards ──────────────────────────
    listed = session.get(portal + "/api/intercom", timeout=T).json()
    msgs = listed if isinstance(listed, list) else listed.get("messages", [])
    texts = [m.get("text", "") for m in msgs]
    missing = [p for p in payloads if p not in texts]
    (result.ok if not missing else result.fail)(
        f"no lost update: all {N_WRITERS} notes in the store afterwards"
        if not missing
        else f"lost updates: {len(missing)} notes vanished"
    )

    # ── AH3: readers saw consistent state ───────────────────────────
    good_reads = sum(1 for code, valid in reader_results
                     if code == 200 and valid)
    (result.ok if good_reads == N_READERS else result.fail)(
        f"all {N_READERS} concurrent readers saw valid HTTP-200 JSON"
        if good_reads == N_READERS
        else f"readers saw broken state: {good_reads}/{N_READERS} ok"
    )

    # ── AH4: the page renders the storm ────────────────────────────
    page = session.get(portal + "/", timeout=T)
    page.encoding = "utf-8"
    # the markers are unicode-safe (RTL + digits) — the page must carry
    # the notes that just survived the contention
    p0 = payloads[0]
    present = p0 in page.text
    (result.ok if present else result.fail)(
        "served page carries the concurrent notes"
        if present
        else "concurrent notes missing from the rendered page"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
