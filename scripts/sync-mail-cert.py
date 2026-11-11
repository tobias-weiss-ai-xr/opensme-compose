#!/usr/bin/env python3
"""Sync the fleet Traefik ACME certificate for the mail hostname into the
Stalwart TLS directory.

Traefik (resolver ``mytlschallenge``) owns the public Let's Encrypt account and
renews certificates inside ``acme.json``.  Stalwart cannot run ACME itself on
this host (Traefik owns :80/:443), so it reads file-backed certificates from
``compose/certs`` instead.  This script bridges the two: it extracts the
certificate for ``MAIL_DOMAIN`` and only restarts Stalwart when the material
actually changed (renewal is roughly every 60 days).

Run by ``opensme-mail-cert.timer``; safe to run by hand.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# All paths are deployment-specific and therefore supplied by the caller
# (systemd unit / shell). Nothing host-specific is baked into this repo.
ACME_JSON = Path(os.environ.get("ACME_JSON", ""))
MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "")
RESOLVER = os.environ.get("ACME_RESOLVER", "mytlschallenge")
# Default: <repo>/certs — the directory mounted into Stalwart by
# mail/stalwart.yml. Override with MAIL_TLS_DIR when running from elsewhere.
OUTDIR = Path(os.environ.get("MAIL_TLS_DIR", "")) or (
    Path(__file__).resolve().parent.parent / "certs"
)
CONTAINER = os.environ.get("STALWART_CONTAINER", "opensme-stalwart")


def die(msg: str) -> None:
    print(f"sync-mail-cert: {msg}", file=sys.stderr)
    raise SystemExit(1)


def find_cert(data: dict, resolver: str, domain: str) -> dict:
    resolvers = [resolver] if resolver in data else list(data)
    for name in resolvers:
        for cert in data.get(name, {}).get("Certificates") or []:
            dom = cert.get("domain") or {}
            names = {dom.get("main")} | set(dom.get("sans") or [])
            if domain in names:
                return cert
    die(f"no certificate for {domain} in {ACME_JSON} (resolvers: {', '.join(resolvers)})")


def as_pem(value: str) -> str:
    """Traefik stores ACME material base64-encoded; accept both forms."""
    if value.lstrip().startswith("-----BEGIN"):
        return value
    return base64.b64decode(value).decode()


def write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text() == content:
        return False
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return True


def main() -> int:
    if not MAIL_DOMAIN:
        die("MAIL_DOMAIN is not set (mail hostname whose certificate to sync)")
    if not str(ACME_JSON) or str(ACME_JSON) == ".":
        die("ACME_JSON is not set (path to the Traefik acme.json store)")
    if not ACME_JSON.is_file():
        die(f"ACME store not found: {ACME_JSON}")
    data = json.loads(ACME_JSON.read_text())
    cert = find_cert(data, RESOLVER, MAIL_DOMAIN)

    cert_pem = as_pem(cert.get("certificate") or "")
    key_pem = as_pem(cert.get("key") or "")
    if "BEGIN CERTIFICATE" not in cert_pem or "PRIVATE KEY" not in key_pem:
        die("certificate/key missing or not PEM")

    OUTDIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(OUTDIR, 0o700)

    changed = write_if_changed(OUTDIR / "fullchain.pem", cert_pem)
    changed |= write_if_changed(OUTDIR / "privkey.pem", key_pem)

    if changed and not os.environ.get("SYNC_NO_RESTART"):
        print(f"sync-mail-cert: updated certificate for {MAIL_DOMAIN}; restarting {CONTAINER}")
        subprocess.run(["docker", "restart", CONTAINER], check=True, stdout=subprocess.DEVNULL)
    elif changed:
        print(f"sync-mail-cert: updated certificate for {MAIL_DOMAIN} (restart skipped)")
    else:
        print(f"sync-mail-cert: certificate for {MAIL_DOMAIN} unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
