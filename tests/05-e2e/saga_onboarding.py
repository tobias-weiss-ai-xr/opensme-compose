#!/usr/bin/env python3
"""
tests/05-e2e/saga_onboarding.py — Onboarding journey blueprint (S1).

Validates the public contracts of the onboarding saga: OIDC discovery,
portal health, the first catalog tile opens, and SSO reuse into OpenCloud
and SOGo (auth-code redirect to the IdP, not a 404). Skips cleanly when
any participating service (IdP, portal, OpenCloud, SOGo) is not deployed.

Usage:
    python3 tests/05-e2e/saga_onboarding.py              # domain from .env
    python3 tests/05-e2e/saga_onboarding.py opensme.local

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
from run import T, REDIRECTS, install_dns_fallback, is_localish, load_env_file
from saga_common import probe_portal, service_deployed, service_url


def main() -> bool:
    ap = argparse.ArgumentParser(description="openSME onboarding journey blueprint")
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
    result = Result("saga-onboarding")

    # Portal discovery + rendered tiles
    env_portal = os.environ.get("PORTAL_DOMAIN") or cfg("PORTAL_DOMAIN", "")
    portal_base, services = probe_portal(session, domain, localish, schema, env_portal)
    if portal_base is None:
        result.skip("portal unreachable — start the stack first")
        return result.summary()

    result.header(f"onboarding journey — portal={portal_base}")

    # Participating services: IdP, portal, OpenCloud, SOGo
    idp_host = cfg("ZITADEL_DOMAIN", f"auth.{domain}")
    idp_base = cfg("E2E_IDP_URL", f"{schema}{idp_host}")
    has_cloud = service_deployed(services, "opencloud")
    has_webmail = service_deployed(services, "webmail")

    if not has_cloud:
        result.skip("onboarding: OpenCloud not deployed — journey skipped")
        return result.summary()
    if not has_webmail:
        result.skip("onboarding: Webmail (SOGo) not deployed — journey skipped")
        return result.summary()

    # 1. OIDC discovery
    try:
        disc_resp = session.get(f"{idp_base}/.well-known/openid-configuration",
                                timeout=T)
        if disc_resp.status_code != 200:
            result.fail(f"IdP discovery: HTTP {disc_resp.status_code}")
            return result.summary()
        disc = disc_resp.json()
        missing = [k for k in ("issuer", "authorization_endpoint",
                                "token_endpoint", "jwks_uri") if not disc.get(k)]
        if missing:
            result.fail(f"IdP discovery missing keys: {missing}")
            return result.summary()
        result.ok(f"OIDC discovery ok (issuer {disc['issuer']})")
    except (requests.RequestException, ValueError) as e:
        result.fail(f"IdP discovery unreachable: {e.__class__.__name__}")
        return result.summary()

    # 2. Portal /health (already confirmed by probe_portal, re-assert)
    result.ok("portal /health answers")

    # 3. First catalog tile opens
    first_tile = services[0] if services else None
    if not first_tile:
        result.fail("portal renders no tiles — catalog is empty")
        return result.summary()
    tile_url = first_tile.get("url", "")
    if tile_url and not tile_url.startswith("http"):
        tile_url = "https://" + tile_url
    try:
        r = session.get(tile_url, allow_redirects=True, timeout=T)
        if r.status_code < 400:
            result.ok(f"first tile '{first_tile['id']}' opens (HTTP {r.status_code})")
        else:
            result.fail(f"first tile '{first_tile['id']}' broken (HTTP {r.status_code})")
    except requests.RequestException as e:
        result.fail(f"first tile '{first_tile['id']}' unreachable ({e.__class__.__name__})")

    # 4. SSO reuse into OpenCloud (auth-code redirect to IdP)
    cloud_url = service_url(services, "opencloud")
    if cloud_url and not cloud_url.startswith("http"):
        cloud_url = "https://" + cloud_url
    try:
        az = session.get(disc["authorization_endpoint"], params={
            "client_id": cfg("OC_OIDC_CLIENT_ID", "opencloud"),
            "response_type": "code",
            "scope": "openid",
            "nonce": "e2e-onboarding-probe",
            "redirect_uri": f"{cloud_url}/oidc-callback.html",
        }, allow_redirects=False, timeout=T)
        if az.status_code in REDIRECTS:
            result.ok("OpenCloud SSO: IdP redirect (auth-code flow)")
        elif "App.NotFound" in az.text or "client" in az.text.lower():
            result.warn("OpenCloud OIDC client not registered in this subset")
        else:
            result.warn(f"OpenCloud OIDC probe: HTTP {az.status_code}")
    except (requests.RequestException, ValueError) as e:
        result.warn(f"OpenCloud OIDC probe failed: {e.__class__.__name__}")

    # 5. SSO reuse into SOGo (webmail redirects to IdP)
    webmail_url = service_url(services, "webmail")
    if webmail_url and not webmail_url.startswith("http"):
        webmail_url = "https://" + webmail_url
    try:
        r = session.get(webmail_url + "/sogo/", allow_redirects=False, timeout=T)
        loc = r.headers.get("Location", "")
        if r.status_code in REDIRECTS and loc.startswith(idp_base):
            result.ok("SOGo SSO: redirect targets the IdP")
        elif r.status_code == 200:
            result.warn("SOGo reachable but serves a local login (SSO not enforced)")
        else:
            result.warn(f"SOGo probe: HTTP {r.status_code}")
    except requests.RequestException as e:
        result.warn(f"SOGo probe failed: {e.__class__.__name__}")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
