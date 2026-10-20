#!/usr/bin/env python3
"""
tests/05-e2e/saga_quote_to_cash.py — Quote-to-cash journey blueprint (S2).

Validates the public contracts of the quote-to-cash saga: the Invoice Ninja
export endpoint and the Paperless-ngx DMS ingest endpoint both answer at the
edge (behind auth walls, not 404). Skips cleanly when either service is not
deployed.

Usage:
    python3 tests/05-e2e/saga_quote_to_cash.py              # domain from .env
    python3 tests/05-e2e/saga_quote_to_cash.py opensme.local

Exit codes: 0 = pass, 1 = failed (skips don't fail).
"""

import argparse
import os
import sys
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import install_dns_fallback, is_localish, load_env_file
from saga_common import probe_portal, service_deployed, service_url, probe_endpoint


def main() -> bool:
    ap = argparse.ArgumentParser(description="openSME quote-to-cash journey blueprint")
    ap.add_argument("domain", nargs="?", default=None, help="stack domain")
    args = ap.parse_args()

    dotenv = load_env_file()

    def cfg(key, default=""):
        return os.environ.get(key) or dotenv.get(key) or default

    domain = (args.domain or cfg("OPENSME_DOMAIN", "localhost")).strip()
    localish = is_localish(domain)
    if localish:
        install_dns_fallback()
        urllib3.disable_warnings()

    schema = "http://" if localish else "https://"

    session = requests.Session()
    session.verify = not localish
    result = Result("saga-quote-to-cash")

    # Portal discovery + rendered tiles
    env_portal = os.environ.get("PORTAL_DOMAIN") or cfg("PORTAL_DOMAIN", "")
    portal_base, services = probe_portal(session, domain, localish, schema, env_portal)
    if portal_base is None:
        result.skip("portal unreachable — start the stack first")
        return result.summary()

    result.header(f"quote-to-cash journey — portal={portal_base}")

    # Participating services: Invoice Ninja, Paperless-ngx
    has_invoice = service_deployed(services, "invoice")
    has_archive = service_deployed(services, "archive")

    if not has_invoice:
        result.skip("quote-to-cash: Invoice Ninja not deployed — journey skipped")
        return result.summary()
    if not has_archive:
        result.skip("quote-to-cash: Paperless-ngx not deployed — journey skipped")
        return result.summary()

    # 1. Invoice Ninja export endpoint (auth-walled, not 404)
    invoice_url = service_url(services, "invoice")
    if invoice_url and not invoice_url.startswith("http"):
        invoice_url = "https://" + invoice_url
    invoice_api = invoice_url.rstrip("/") + "/api/v1/invoices"
    ok, detail = probe_endpoint(session, invoice_api, allow_redirects=False)
    if ok:
        result.ok(f"invoice export API answers (HTTP {detail})")
    else:
        result.fail(f"invoice export API: {detail}")

    # 2. Paperless-ngx DMS ingest endpoint (auth-walled, not 404)
    archive_url = service_url(services, "archive")
    if archive_url and not archive_url.startswith("http"):
        archive_url = "https://" + archive_url
    archive_api = archive_url.rstrip("/") + "/api/documents/"
    ok, detail = probe_endpoint(session, archive_api, allow_redirects=False)
    if ok:
        result.ok(f"DMS ingest API answers (HTTP {detail})")
    else:
        result.fail(f"DMS ingest API: {detail}")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
