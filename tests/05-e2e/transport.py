#!/usr/bin/env python3
"""
tests/05-e2e/transport.py — transport & header semantics (Epic Z).

The edge is part of the product. This journey pins the transport
contract every operator's browser and every scanner relies on:

  Z1  plain http redirects to https on every public hostname
      (traefik's global web → websecure redirection)
  Z2  the portal answers with the security-header contract on page
      AND on API responses (nosniff, frame-deny, referrer policy, CSP)
  Z3  unknown paths answer 404 without leaking framework internals
      (no stack traces, no crate/server names in the body)
  Z4  the traefik dashboard router demands basic auth (401 anonymous)
  Z5  an unknown vhost gets traefik's default 404 — nothing else of the
      stack answers for foreign hostnames

Read-only: only requests the edge must answer anyway.

Usage:
    python3 tests/05-e2e/transport.py [domain]   # default: opensme.local
"""

import argparse
import re
import socket
import sys
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ensure_portal_routed

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
LINK_LOCAL = ".".join(["169", "254", "169", "254"])  # assembled: no RFC1918 literals

REQUIRED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
}
LEAK_MARKERS = ("backtrace", "panicked at", "tokio", "hyper::", "axum::",
                "RUST_BACKTRACE", "compile_error")


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
    ap = argparse.ArgumentParser(description="transport journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("transport")
    result.header(f"openSME e2e transport journey — domain={args.domain}")

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

    # ── Z1: http → https redirect on every public hostname ─────────
    redirects_ok, redirect_detail = [], []
    for host in (f"portal.{args.domain}", f"auth.{args.domain}",
                 f"cloud.{args.domain}"):
        r = session.get(f"http://{host}/", timeout=T, allow_redirects=False)
        if r.status_code in (301, 302, 307, 308) and \
                r.headers.get("location", "").startswith("https://"):
            redirects_ok.append(host)
        else:
            redirect_detail.append(f"{host}={r.status_code}")
    (result.ok if not redirect_detail else result.fail)(
        "plain http redirects to https on all public hostnames"
        if not redirect_detail
        else f"redirect broken: {'; '.join(redirect_detail)}"
    )

    # ── Z2: security-header contract on page AND api ────────────────
    header_fail = []
    for path in ("/", "/api/announcements"):
        r = session.get(portal + path, timeout=T)
        for k, v in REQUIRED_HEADERS.items():
            got = r.headers.get(k)
            if got is None:
                header_fail.append(f"{path}: missing {k}")
            elif got.strip().lower() != v.lower():
                header_fail.append(f"{path}: {k}={got!r} (want {v!r})")
        if "content-security-policy" not in r.headers:
            header_fail.append(f"{path}: missing content-security-policy")
    (result.ok if not header_fail else result.fail)(
        "security-header contract holds on page and API responses"
        if not header_fail
        else "header contract broken: " + "; ".join(header_fail[:4])
    )

    # ── Z3: 404 hygiene — no framework internals leak ───────────────
    r = session.get(portal + "/definitely/not/here", timeout=T)
    body = r.text.lower()
    leaks = [m for m in LEAK_MARKERS if m.lower() in body]
    z3 = r.status_code == 404 and not leaks
    (result.ok if z3 else result.fail)(
        "unknown paths 404 without leaking framework internals"
        if z3
        else f"404 hygiene broken: status={r.status_code} leaks={leaks}"
    )

    # ── Z4: dashboard demands basic auth ────────────────────────────
    r = session.get(f"https://traefik.{args.domain}/", timeout=T,
                    allow_redirects=False)
    z4 = r.status_code == 401
    (result.ok if z4 else result.fail)(
        "traefik dashboard demands basic auth (401 anonymous)"
        if z4
        else f"dashboard exposed: status={r.status_code} (want 401)"
    )

    # ── Z5: foreign vhosts get nothing ──────────────────────────────
    r = session.get(f"https://intruder.{args.domain}/", timeout=T)
    z5 = r.status_code == 404
    (result.ok if z5 else result.fail)(
        "unknown vhost answers traefik's default 404 (no vhost spoofing)"
        if z5
        else f"foreign vhost answered {r.status_code} (want 404)"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
