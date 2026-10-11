#!/usr/bin/env python3
"""
tests/05-e2e/logout.py — portal logout & single sign-out journey (Epic A5 / LO-1).

Signs the demo user in through the IdP (Zitadel v2 Session API, same as
run.py), plants the resulting ID token into the portal session cookie
(what an authenticated portal browser carries), then drives the portal's
logout contract end to end:

  K1  POST /api/logout clears the portal session cookie and answers a
      303 redirect to the IdP's OIDC end-session endpoint, carrying the
      id_token_hint and the portal's /logged-out post-logout URI.
  K2  Following the chain lands on the portal's /logged-out page — the
      IdP completed the sign-out and bounced the browser back.
  K3  The shared IdP session is dead: a fresh auth request can no longer
      be finalized with it (replay rejected by the IdP).
  K4  Single log-out reaches OpenCloud: before the portal logout a second
      relying party (an OIDC app standing in for OpenCloud's client) is
      authorized from the shared session; after the portal logout the
      same finalize is rejected — the portal logout signed the cloud out.

No browser is involved; the login-client PAT authorizes session creation,
password checks and auth-request finalization (see tests/05-e2e/run.py).

Usage:
    python3 tests/05-e2e/logout.py [domain]     # default: opensme.local

Exit codes: 0 = pass, 1 = one or more checks failed (skips don't fail).
"""

import argparse
import os
import re
import subprocess
import sys
import urllib3
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ensure_portal_routed

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import (  # noqa: E402  (path bootstrap before import)
    REDIRECTS,
    T,
    authorize_url,
    bootstrap_app,
    cleanup_app,
    fetch_seeded,
    finalize,
    install_dns_fallback,
    is_localish,
    lc_headers,
    load_env_file,
    resolve_user_id,
    run_auth_code_flow,
)

SESSION_COOKIE = "OPENSMESESSION"
DEMO_LOGIN = "zitadel-admin"
DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"


def compose_args() -> list[str]:
    cf = os.environ.get("COMPOSE_FILE") or DEMO_SET
    args: list[str] = []
    for f in cf.split(os.pathsep):
        if f:
            args += ["-f", f]
    return args


def rebuild_portal() -> None:
    """Serve the portal code under test (same convention as intercom.py)."""
    subprocess.run(["docker", "compose"] + compose_args() + ["up", "-d", "--build", "portal"],
                   check=True, capture_output=True, timeout=600)


def bootstrap_cloud_app(session, idp_base, pat, callback, post_logout):
    """A second throw-away OIDC app standing in for OpenCloud's client.

    Lives in its own project so it never collides with the portal app
    bootstrap (bootstrap_app cleans up apps named 'e2e-sso')."""
    import requests

    def mgmt(method, path, payload=None):
        return session.request(
            method, idp_base + path,
            json=payload if payload is not None else None,
            headers={"Authorization": f"Bearer {pat}"}, timeout=T,
        )

    r = mgmt("POST", "/management/v1/projects/_search", {})
    if r.status_code != 200:
        raise RuntimeError(f"project search failed: HTTP {r.status_code}")
    pid = next((p["id"] for p in r.json().get("result", [])
                if p.get("name") == "e2e-cloud"), None)
    if pid is None:
        r = mgmt("POST", "/management/v1/projects", {"name": "e2e-cloud"})
        if r.status_code != 200:
            raise RuntimeError(f"project add failed: HTTP {r.status_code}")
        pid = r.json().get("id")
    else:
        r = mgmt("POST", f"/management/v1/projects/{pid}/apps/_search", {})
        if r.status_code == 200:
            for app in r.json().get("result", []):
                mgmt("DELETE", f"/management/v1/projects/{pid}/apps/{app['id']}")
    r = mgmt("POST", f"/management/v1/projects/{pid}/apps/oidc", {
        "name": "e2e-cloud-sso",
        "redirectUris": [callback],
        "postLogoutRedirectUris": [post_logout],
        "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
        "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
        "appType": "OIDC_APP_TYPE_WEB",
        "authMethodType": "OIDC_AUTH_METHOD_TYPE_NONE",
        "devMode": True,
    })
    if r.status_code != 200:
        raise RuntimeError(f"cloud app add failed: HTTP {r.status_code}")
    data = r.json()
    return data.get("clientId"), pid, data.get("appId")


def cloud_ready(session, cloud_base):
    """Soft gate: the OpenCloud leg only runs when the overlay is up."""
    try:
        return session.get(cloud_base + "/", timeout=T,
                           allow_redirects=False).status_code < 500
    except Exception:
        return False


def main() -> bool:
    import requests

    ap = argparse.ArgumentParser(description="openSME portal logout & SLO journey")
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
    idp_base = f"{schema}{cfg('ZITADEL_DOMAIN', f'auth.{domain}')}"
    # The portal logout contract runs over HTTPS: Traefik bounces plain-http
    # with a proxy redirect that would mask the endpoint's own 303.
    portal_base = (
        "http://127.0.0.1:8080" if domain in ("localhost", "127.0.0.1")
        else f"https://{cfg('PORTAL_DOMAIN', f'portal.{domain}')}"
    )
    cloud_base = f"{schema}{cfg('OPENCLOUD_DOMAIN', f'cloud.{domain}')}"

    session = requests.Session()
    session.verify = not localish

    result = Result("logout")
    result.header(
        f"portal logout & single sign-out — domain={domain} "
        f"idp={idp_base} portal={portal_base}"
    )

    if not _probe(session, idp_base + "/debug/ready"):
        result.fail("stack unreachable — start it first (./scripts/demo.sh or make up)")
        return result.summary()

    # Serve the current portal code (with /api/logout) like the other journeys
    # drive the portal lifecycle; wait until Traefik routes the new container.
    try:
        rebuild_portal()
        if not ensure_portal_routed(session, portal_base):
            result.fail("portal not reachable after rebuild")
            return result.summary()
        result.ok("portal rebuilt and routed (serving /api/logout)")
    except Exception as e:
        result.warn(f"portal rebuild skipped ({type(e).__name__}: {e}) — "
                    "testing the running portal as-is")

    try:
        disc = session.get(f"{idp_base}/.well-known/openid-configuration",
                           timeout=T).json()
    except Exception:
        disc = None
    if not disc or not disc.get("end_session_endpoint"):
        result.fail("IdP discovery unreachable — cannot verify the end-session contract")
        return result.summary()
    result.ok(f"IdP end-session endpoint: {disc['end_session_endpoint']}")

    # ── bootstrap a throw-away OIDC app whose post-logout URI is the portal ──
    pat = cfg("E2E_ZITADEL_PAT") or fetch_seeded("/machinekey/pat")
    if not pat:
        result.skip("no machine PAT reachable — set E2E_ZITADEL_PAT or start the stack")
        return result.summary()
    post_logout = f"{portal_base}/logged-out"
    callback = f"{idp_base}/e2e-callback"
    try:
        client_id, pid, app_id = bootstrap_app(session, idp_base, pat, callback, post_logout)
        result.ok(f"bootstrapped throw-away OIDC app (post-logout → {post_logout})")
    except Exception as e:
        result.skip(f"OIDC app bootstrap failed: {e}")
        return result.summary()

    lc_pat = cfg("E2E_ZITADEL_LOGINCLIENT_PAT") or fetch_seeded("/machinekey/login-client.pat")
    client_uid = resolve_user_id(session, idp_base, pat or lc_pat, "opensme-login-client")
    login_uid = resolve_user_id(session, idp_base, pat or lc_pat, DEMO_LOGIN)
    pw = cfg("ZITADEL_ADMIN_PASSWORD")
    if not lc_pat or not client_uid or not login_uid or not pw:
        result.skip("login client / demo user credentials missing "
                    "(E2E_ZITADEL_LOGINCLIENT_PAT / ZITADEL_ADMIN_PASSWORD)")
        return result.summary()

    # ── sign in: get an ID token + the shared IdP session ────────────────────
    try:
        idt, sess, _ = run_auth_code_flow(
            result, session, disc, client_id, None, idp_base,
            lc_pat, client_uid, DEMO_LOGIN, login_uid, pw, callback,
        )
    except Exception as e:
        result.fail(f"login flow failed: {e}")
        return result.summary()
    result.ok("signed in — ID token + shared IdP session obtained")

    # ── K4 pre: a second RP (stand-in for OpenCloud's client) is authorized
    #           from the shared session while the user is still logged in ─────
    cloud_flow = None
    cc = cpid = capp = None
    if cloud_ready(session, cloud_base):
        try:
            cc, cpid, capp = bootstrap_cloud_app(session, idp_base, pat,
                                                 callback, post_logout)
            authz, _, _, _ = authorize_url(disc, cc, callback)
            r = session.get(authz, headers=lc_headers(lc_pat, client_uid),
                            allow_redirects=False, timeout=T)
            m = re.search(r"authRequest=([A-Za-z0-9_:-]+)",
                          r.headers.get("Location", ""))
            if not m:
                raise RuntimeError("no auth request id for the cloud app")
            finalize(session, idp_base, lc_pat, client_uid, m.group(1),
                     sess["id"], sess["token"])
            result.ok("OpenCloud app authorized from the shared session (pre-logout)")
            cloud_flow = (cc, cpid, capp)
        except Exception as e:
            result.warn(f"OpenCloud pre-check skipped: {e}")
            cloud_flow = None
            if cpid:
                try:
                    cleanup_app(session, idp_base, pat, cpid, capp)
                except Exception:
                    pass

    # ── K1: the portal logout contract ───────────────────────────────────────
    portal_host = (urlparse(portal_base).hostname or "").split(":")[0]
    session.cookies.set(SESSION_COOKIE, idt, domain=portal_host)
    try:
        r = session.post(portal_base + "/api/logout", allow_redirects=False, timeout=T)
    except Exception as e:
        result.fail(f"POST /api/logout unreachable: {type(e).__name__}: {e}")
        return result.summary()

    if r.status_code not in REDIRECTS:
        result.fail(f"POST /api/logout: HTTP {r.status_code} (expected a redirect)")
        return result.summary()
    loc = r.headers.get("Location", "")
    if not loc.startswith(disc["end_session_endpoint"]):
        result.fail(f"logout redirect target wrong: {loc[:100]}")
        return result.summary()
    qs = parse_qs(urlparse(loc).query)
    hint_ok = qs.get("id_token_hint", [None])[0] == idt
    portal_ok = qs.get("post_logout_redirect_uri", [None])[0] == f"{portal_base}/logged-out"
    if hint_ok and portal_ok:
        result.ok("logout redirects to end-session with id_token_hint + portal callback")
    else:
        result.fail(f"end-session params wrong: hint={hint_ok} portal={portal_ok}")
    set_cookie = r.headers.get("Set-Cookie", "")
    if SESSION_COOKIE in set_cookie and "max-age=0" in set_cookie.lower():
        result.ok("portal session cookie cleared (Max-Age=0)")
    else:
        result.fail(f"portal session cookie not cleared: {set_cookie[:120]!r}")

    # ── K2: the IdP completes sign-out and bounces back to /logged-out ───────
    try:
        last = session.get(loc, timeout=T, allow_redirects=True)
        if last.url.rstrip("/").endswith("/logged-out") and last.status_code == 200:
            result.ok("end-session bounced the browser back to the portal /logged-out")
        else:
            result.fail(f"end-session chain ended at {last.url[:90]} "
                        f"(HTTP {last.status_code})")
    except Exception as e:
        result.fail(f"following end-session failed: {type(e).__name__}: {e}")

    # ── K3: the shared IdP session is dead ────────────────────────────────────
    authz, _, _, _ = authorize_url(disc, client_id, callback)
    r = session.get(authz, headers=lc_headers(lc_pat, client_uid),
                    allow_redirects=False, timeout=T)
    m = re.search(r"authRequest=([A-Za-z0-9_:-]+)", r.headers.get("Location", ""))
    if not m:
        result.warn("could not open a fresh auth request after logout")
    else:
        try:
            finalize(session, idp_base, lc_pat, client_uid, m.group(1),
                     sess["id"], sess["token"])
            result.fail("logout ineffective: dead session still finalizes auth requests")
        except Exception:
            result.ok("logout: shared IdP session terminated (replay rejected)")

    # ── K4 post: the portal logout reached the OpenCloud leg ─────────────────
    if cloud_flow:
        cc, cpid, capp = cloud_flow
        try:
            authz, _, _, _ = authorize_url(disc, cc, callback)
            r = session.get(authz, headers=lc_headers(lc_pat, client_uid),
                            allow_redirects=False, timeout=T)
            m = re.search(r"authRequest=([A-Za-z0-9_:-]+)",
                          r.headers.get("Location", ""))
            if not m:
                result.warn("OpenCloud post-logout probe: no auth request id")
            else:
                finalize(session, idp_base, lc_pat, client_uid, m.group(1),
                         sess["id"], sess["token"])
                result.fail("SLO ineffective: OpenCloud app still finalizes")
        except Exception:
            result.ok("single log-out: OpenCloud app rejected after portal logout")
        finally:
            cleanup_app(session, idp_base, pat, cpid, capp)
    else:
        result.skip("OpenCloud leg skipped (overlay not routed / pre-check unavailable)")
    cleanup_app(session, idp_base, pat, pid, app_id)

    return result.summary()


def _probe(session, url) -> bool:
    try:
        return session.get(url, timeout=5, allow_redirects=True).status_code < 500
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
