#!/usr/bin/env python3
"""
tests/05-e2e/tls_contract.py — the crypto edge contract (Epic AG).

The stack is TLS-only. A contract nobody handshake-tested is a wish.
Measured with the stdlib ssl module — no openssl CLI dependency:

  AG1  legacy TLS (1.0/1.1) is REFUSED at the edge
  AG2  modern TLS (1.2 and 1.3) connects
  AG3  every response carries HSTS (max-age ≥ 1 year) — browsers that
       have seen the site once never even try plain http again
  AG4  the served certificate is inside its validity window (not
       expired, not yet valid) — catches swapped/stale certs

Usage:
    python3 tests/05-e2e/tls_contract.py [domain]  # default: opensme.local
"""

import argparse
import datetime
import re
import socket
import ssl
import sys
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ensure_portal_routed

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")


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


def handshake(port: int, host: str, ip: str,
              max_proto: ssl.TLSVersion) -> tuple[bool, str]:
    """Try a TLS handshake capped at max_proto. Returns (ok, detail)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = max_proto
    try:
        with socket.create_connection((ip, port), timeout=T) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                return True, tls.version() or "?"
    except (ssl.SSLError, OSError) as e:
        return False, e.__class__.__name__


def get_cert(ip: str, host: str) -> dict:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((ip, 443), timeout=T) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
            # parse validity from the DER ourselves (getpeercert() dict
            # is empty for CERT_NONE) — the ASN.1 UTCTime for notBefore/
            # notAfter is stable enough via ssl.DER_cert_to_PEM + ssl
            # private helpers is overkill; use the handshake cert dict
            # when present, else parse via ssl._ssl… no: re-derive with
            # openssl-style via ssl module only is not exposed, so we
            # use the standard DER layout probe via ssl.SSLObject
            return {"version": tls.version(),
                    "pem": ssl.DER_cert_to_PEM_cert(der)}


def main() -> int:
    ap = argparse.ArgumentParser(description="tls contract journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("tls-contract")
    result.header(f"openSME e2e tls contract journey — domain={args.domain}")

    names = subprocess_names()
    if not any("opensme-portal" in n for n in names) or \
            not any("opensme-traefik" in n for n in names):
        result.skip("portal/traefik not running (start the stack first)")
        print()
        return 0

    host = f"portal.{args.domain}"
    portal = f"https://{host}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False
    if not ensure_portal_routed(session, portal):
        result.fail("portal unreachable via traefik even after resync")
        return 1

    ip = "127.0.0.1"

    # ── AG1: legacy TLS refused ─────────────────────────────────────
    legacy = [handshake(443, host, ip, p) for p in
              (ssl.TLSVersion.MINIMUM_SUPPORTED, ssl.TLSVersion.TLSv1_1)]
    refused = all(not ok for ok, _ in legacy)
    accepted = [f"{label}:{v}" for (ok, v), label in zip(legacy, ("min-cap", "tls1.1-cap")) if ok]
    (result.ok if refused else result.fail)(
        "legacy TLS (≤1.1) refused at the edge"
        if refused
        else f"legacy TLS accepted: {accepted}"
    )

    # ── AG2: modern TLS connects ────────────────────────────────────
    modern = [handshake(443, host, ip, p) for p in
              (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.MAXIMUM_SUPPORTED)]
    works = all(ok for ok, _ in modern)
    (result.ok if works else result.fail)(
        f"modern TLS connects ({modern[0][1]} and {modern[1][1]})"
        if works
        else f"modern TLS broken: {modern}"
    )

    # ── AG3: HSTS on the page ───────────────────────────────────────
    r = session.get(portal + "/", timeout=T)
    hsts = r.headers.get("strict-transport-security", "")
    max_age = re.search(r"max-age=(\d+)", hsts)
    good = bool(max_age) and int(max_age.group(1)) >= 31536000
    (result.ok if good else result.fail)(
        f"HSTS present: {hsts or '(missing)'}"
        if good
        else f"HSTS missing or too short: '{hsts}'"
    )

    # ── AG4: certificate inside its validity window ─────────────────
    # (parsed by the host's openssl — same tool an operator would use)
    in_window, detail = False, ""
    try:
        import subprocess
        pem = get_cert(ip, host)["pem"]
        proc = subprocess.run(
            ["openssl", "x509", "-noout", "-startdate", "-enddate"],
            input=pem, capture_output=True, text=True, timeout=30)
        nb_s = na_s = ""
        for line in proc.stdout.splitlines():
            if line.startswith("notBefore="):
                nb_s = line.split("=", 1)[1]
            elif line.startswith("notAfter="):
                na_s = line.split("=", 1)[1]
        fmt = "%b %d %H:%M:%S %Y %Z"
        nb = datetime.datetime.strptime(re.sub(r"\s+", " ", nb_s).strip(), fmt) \
            .replace(tzinfo=datetime.timezone.utc)
        na = datetime.datetime.strptime(re.sub(r"\s+", " ", na_s).strip(), fmt) \
            .replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        in_window = nb <= now <= na
        detail = f"valid {nb:%Y-%m-%d} → {na:%Y-%m-%d}"
    except Exception as e:
        detail = f"{e.__class__.__name__}: {e}"
    (result.ok if in_window else result.fail)(
        f"certificate inside validity window ({detail})"
        if in_window
        else f"certificate window broken: {detail}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


def subprocess_names() -> list[str]:
    import subprocess
    return subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                          capture_output=True, text=True).stdout.split()


if __name__ == "__main__":
    sys.exit(main())
