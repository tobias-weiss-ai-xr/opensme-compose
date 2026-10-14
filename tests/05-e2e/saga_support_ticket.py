#!/usr/bin/env python3
"""
tests/05-e2e/saga_support_ticket.py — Support-ticket journey blueprint (S4).

Validates the public contract of the support-ticket saga: the ticketing
service (Nosdesk) answers at the edge (HTTP < 400, not 404). Skips cleanly
when the ticketing service is not deployed.

Usage:
    python3 tests/05-e2e/saga_support_ticket.py              # domain from .env
    python3 tests/05-e2e/saga_support_ticket.py opensme.local

Exit codes: 0 = pass, 1 = failed (skips don't fail).
"""

import argparse
import os
import sys
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import T, install_dns_fallback, is_localish, load_env_file  # noqa: E402
from saga_common import probe_portal, service_deployed, service_url, probe_endpoint  # noqa: E402


def main() -> bool:
    ap = argparse.ArgumentParser(description="openSME support-ticket journey blueprint")
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
    result = Result("saga-support-ticket")

    # Portal discovery + rendered tiles
    env_portal = os.environ.get("PORTAL_DOMAIN") or cfg("PORTAL_DOMAIN", "")
    portal_base, services = probe_portal(session, domain, localish, schema, env_portal)
    if portal_base is None:
        result.skip("portal unreachable — start the stack first")
        return result.summary()

    result.header(f"support-ticket journey — portal={portal_base}")

    # Participating service: ticketing (Nosdesk)
    if not service_deployed(services, "support"):
        result.skip("support-ticket: ticketing service not deployed — journey skipped")
        return result.summary()

    # 1. Ticketing service answers at the edge
    ticketing_url = service_url(services, "support")
    if ticketing_url and not ticketing_url.startswith("http"):
        ticketing_url = "https://" + ticketing_url
    ok, detail = probe_endpoint(session, ticketing_url, allow_redirects=True)
    if ok:
        result.ok(f"ticketing service answers (HTTP {detail})")
    else:
        result.fail(f"ticketing service: {detail}")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
