#!/usr/bin/env python3
"""
tests/05-e2e/saga_publishing.py — Publishing journey blueprint (S5).

Validates the public contracts of the publishing saga: the CMS publish API
(/admin) answers (auth-walled or redirect, not 404) and the published site
is publicly reachable (HTTP 200). Skips cleanly when the CMS service is not
deployed.

Usage:
    python3 tests/05-e2e/saga_publishing.py              # domain from .env
    python3 tests/05-e2e/saga_publishing.py opensme.local

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
    ap = argparse.ArgumentParser(description="openSME publishing journey blueprint")
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
    result = Result("saga-publishing")

    # Portal discovery + rendered tiles
    env_portal = os.environ.get("PORTAL_DOMAIN") or cfg("PORTAL_DOMAIN", "")
    portal_base, services = probe_portal(session, domain, localish, schema, env_portal)
    if portal_base is None:
        result.skip("portal unreachable — start the stack first")
        return result.summary()

    result.header(f"publishing journey — portal={portal_base}")

    # Participating service: CMS (website tile)
    if not service_deployed(services, "website"):
        result.skip("publishing: CMS (website) not deployed — journey skipped")
        return result.summary()

    # 1. CMS publish API (/admin) — auth-walled or redirect, not 404
    cms_url = service_url(services, "website")
    if cms_url and not cms_url.startswith("http"):
        cms_url = "https://" + cms_url
    admin_url = cms_url.rstrip("/") + "/admin"
    ok, detail = probe_endpoint(session, admin_url, allow_redirects=False)
    if ok:
        result.ok(f"CMS publish API answers (HTTP {detail})")
    else:
        result.fail(f"CMS publish API: {detail}")

    # 2. Published site is publicly reachable (HTTP 200)
    ok, detail = probe_endpoint(session, cms_url, allow_redirects=True,
                                expect_status=(200, 301, 302, 303, 307, 308))
    if ok:
        result.ok(f"published site reachable (HTTP {detail})")
    else:
        result.fail(f"published site: {detail}")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
