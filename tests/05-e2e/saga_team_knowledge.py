#!/usr/bin/env python3
"""
tests/05-e2e/saga_team_knowledge.py — Team-knowledge journey blueprint (S6).

Validates the public contract of the team-knowledge saga: the Notes API
(/api/v1.0/documents/) answers at the edge (behind an auth wall, not 404).
Skips cleanly when the Notes service is not deployed.

Usage:
    python3 tests/05-e2e/saga_team_knowledge.py              # domain from .env
    python3 tests/05-e2e/saga_team_knowledge.py opensme.local

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
    ap = argparse.ArgumentParser(description="openSME team-knowledge journey blueprint")
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
    result = Result("saga-team-knowledge")

    # Portal discovery + rendered tiles
    env_portal = os.environ.get("PORTAL_DOMAIN") or cfg("PORTAL_DOMAIN", "")
    portal_base, services = probe_portal(session, domain, localish, schema, env_portal)
    if portal_base is None:
        result.skip("portal unreachable — start the stack first")
        return result.summary()

    result.header(f"team-knowledge journey — portal={portal_base}")

    # Participating service: Notes (notes tile)
    if not service_deployed(services, "notes"):
        result.skip("team-knowledge: Notes not deployed — journey skipped")
        return result.summary()

    # 1. Notes API (documents endpoint) — auth-walled, not 404
    notes_url = service_url(services, "notes")
    if notes_url and not notes_url.startswith("http"):
        notes_url = "https://" + notes_url
    notes_api = notes_url.rstrip("/") + "/api/v1.0/documents/"
    ok, detail = probe_endpoint(session, notes_api, allow_redirects=False)
    if ok:
        result.ok(f"Notes API answers (HTTP {detail})")
    else:
        result.fail(f"Notes API: {detail}")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
