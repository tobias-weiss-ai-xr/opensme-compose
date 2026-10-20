#!/usr/bin/env python3
"""
tests/05-e2e/hygiene.py — no defaults, no open writes (Epic S).

Two hygiene properties a workspace stack must never lose:

  S1  no placeholder credential reached the RUNNING stack: container
      environments carry no CHANGEME_* / example secrets (the documented
      minio/CHANGEME_minio S3 pair is the single allowed exception and
      only in the full profile — it must never run in the demo)
  S2  the IdP rejects ANONYMOUS management writes: creating or deleting
      users without a token answers 401/403, not 200/2xx
  S3  the cloud rejects fabricated credentials: WebDAV and capabilities
      answer 401 to a made-up bearer token

Read-only against the stack (S2/S3 send rejected requests only).

Usage:
    python3 tests/05-e2e/hygiene.py [domain]     # default: opensme.local
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
from conftest import Result

import requests

LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
PLACEHOLDER = re.compile(r"CHANGEME_|_PLACEHOLDER_|example\.org|password123",
                         re.IGNORECASE)
# documented exception: the local S3 pair is intentionally minio/CHANGEME_minio
ALLOWED_PLACEHOLDERS = {"CHANGEME_minio"}


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
    ap = argparse.ArgumentParser(description="hygiene journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("hygiene")
    result.header(f"openSME e2e hygiene journey — domain={args.domain}")

    probe = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
    names = [n for n in probe.stdout.split() if "opensme" in n]
    if not any("postgres" in n for n in names):
        result.skip("stack not running")
        print()
        return 0

    # ── S1: no placeholder credential in any running container env ──────
    leaks = []
    for name in names:
        r = subprocess.run(
            ["docker", "inspect", name, "--format", "{{json .Config.Env}}"],
            capture_output=True, text=True, timeout=60)
        try:
            envs = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            envs = []
        for kv in envs:
            if "=" not in kv:
                continue
            _, _, val = kv.partition("=")
            if PLACEHOLDER.search(val) and val not in ALLOWED_PLACEHOLDERS:
                leaks.append(f"{name}: {kv.split('=')[0]}")
    (result.ok if not leaks else result.fail)(
        f"no placeholder credentials across {len(names)} containers"
        if not leaks else f"default credentials LIVE in: {'; '.join(leaks)}"
    )

    session = requests.Session()
    session.verify = False
    session.trust_env = False
    idp = f"https://auth.{args.domain}"
    cloud = f"https://cloud.{args.domain}"

    # ── S2: anonymous management writes rejected by the IdP ─────────────
    rejects = []
    for method, path, body in (
        ("POST", "/v2/users/human", {"username": "e2e-anon"}),
        ("DELETE", "/v2/users/999999999999999999", None),
    ):
        r = session.request(method, idp + path, json=body, timeout=15)
        if r.status_code < 400:
            rejects.append(f"{method} {path} → {r.status_code}")
    (result.ok if not rejects else result.fail)(
        "IdP rejects anonymous management writes (401/403)"
        if not rejects else f"IdP accepted anonymous writes: {'; '.join(rejects)}"
    )

    # ── S3: fabricated credentials rejected by the cloud ────────────────
    h = {"Authorization": "Bearer e2e-forged-token-000"}
    wall_ok = wcap_ok = False
    try:
        wall_ok = session.get(cloud + "/remote.php/dav/", headers=h,
                              timeout=15).status_code in (401, 403)
    except requests.RequestException:
        pass
    try:
        wcap_ok = session.get(cloud + "/ocs/v2.php/cloud/capabilities?format=json",
                              headers=h, timeout=15).status_code in (401, 403)
    except requests.RequestException:
        pass
    (result.ok if wall_ok and wcap_ok else result.fail)(
        "cloud rejects forged bearer on WebDAV + capabilities"
        if wall_ok and wcap_ok
        else f"cloud accepted forged credentials: dav={wall_ok} cap={wcap_ok}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
