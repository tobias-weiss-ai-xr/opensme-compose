#!/usr/bin/env python3
"""
tests/05-e2e/session_lifecycle.py — Epic S: OIDC session lifecycle.

A single sign-on stack only "works" while the session that carries the
user is alive, refreshable and shared. This journey drives the three
properties that make a session a session (same conventions as
tests/05-e2e/run.py: real HTTP against a RUNNING stack, real credentials
from the seeded Zitadel machine keys, no browser):

  S1  Session timeout    — every credential the IdP issues is time-bounded
                           and the bounds agree end to end:
                           token response `expires_in`, the id_token JWT
                           `exp`/`iat` claims, and a currently-valid token.
  S2  Token auto-refresh — offline_access is honored: the first exchange
                           returns a refresh_token, the refresh grant mints
                           a NEW access token with a rotated refresh token
                           and a fresh lifetime — without any re-login —
                           and the refreshed token is live at userinfo.
  S3  Cross-service SSO  — ONE IdP session covers TWO relying parties
                           (portal app + the OpenCloud client): after the
                           portal login, the OpenCloud authorize is
                           finalized with the SAME session and issues a
                           code — no second credential check anywhere.

No waits on real timeouts: S1 verifies the timeout CONTRACT the whole
stack relies on (finite, consistent, currently-valid lifetimes), which is
all an E2E suite can honestly assert without sleeping for hours.

Seeded credentials (same discovery as tests/05-e2e/run.py): the automation
machine PAT (Management API) authorizes app bootstrap + user provisioning;
the login-client PAT (IAM_LOGIN_CLIENT) drives the v2 Session API the
Login v2 UI itself uses.

Usage:
    python3 tests/05-e2e/session_lifecycle.py [domain]     # default: opensme.local
    python3 tests/05-e2e/session_lifecycle.py opensme.local

Exit codes: 0 = pass, 1 = one or more checks failed (skips don't fail).
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

T = 15  # per-request timeout (s)
REDIRECTS = (301, 302, 303, 307, 308)
LOCALISH_SUFFIXES = (".local", ".localhost", ".test", ".invalid")
LOGIN_CLIENT_USER = "opensme-login-client"
VERIFY = True

# The throw-away pieces this journey owns (isolated from run.py's e2e-sso
# and from the other e2e journeys so parallel test runs never collide).
APP_PROJECT = "e2e-session-lifecycle"
APP_NAME = "session-lifecycle"
SESSION_USER = "e2e-session"

# S1 tolerances: the IdP may round exp to the nearest second and the clock
# between this host and the IdP may drift a little.
SKEW_S = 60
MAX_SANE_LIFETIME_S = 7 * 24 * 3600


class E2EError(Exception):
    """A flow-level failure (vs. an infrastructure skip)."""


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

def load_env_file() -> dict:
    data = {}
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def is_localish(domain: str) -> bool:
    return domain in ("localhost", "127.0.0.1") or any(
        domain.endswith(s) for s in LOCALISH_SUFFIXES
    )


def install_dns_fallback() -> None:
    """Map unresolvable hostnames to loopback (local demo domains reach
    the local Traefik through this — same trick as tests/05-e2e/run.py)."""
    original = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        try:
            return original(host, *args, **kwargs)
        except socket.gaierror:
            if (
                isinstance(host, str)
                and re.fullmatch(r"[a-zA-Z0-9.-]+", host)
                and any(host.endswith(s) for s in LOCALISH_SUFFIXES)
            ):
                return original("127.0.0.1", *args, **kwargs)
            raise

    socket.getaddrinfo = patched


# ─────────────────────────────────────────────────────────────────────────────
# PKCE + JWT helpers
# ─────────────────────────────────────────────────────────────────────────────

def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64u_json(s: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))


def make_pkce():
    verifier = b64url(secrets.token_bytes(48))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def authorize_url(disc, client_id, callback, scope="openid profile email offline_access",
                  extra=None):
    verifier, challenge = make_pkce()
    qs = {
        "client_id": client_id,
        "redirect_uri": callback,
        "response_type": "code",
        "scope": scope,
        "state": secrets.token_urlsafe(16),
        "nonce": secrets.token_urlsafe(16),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if extra:
        qs.update(extra)
    return f"{disc['authorization_endpoint']}?{urlencode(qs)}", verifier


# ─────────────────────────────────────────────────────────────────────────────
# Seeded machine credentials (docker compose cp from the running zitadel)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_seeded(container_path):
    """Read a seeded credential file from the running zitadel container."""
    root = Path(__file__).resolve().parent.parent.parent
    sets = []
    if os.environ.get("COMPOSE_FILE"):
        sets.append(os.environ["COMPOSE_FILE"].split(os.pathsep))
    sets.append([
        "docker-compose.yml",
        "idm/zitadel.yml",
        "opencloud/opencloud.yml",
        "profiles/demo.dev.yml",
    ])
    sets.append(["docker-compose.yml", "idm/zitadel.yml"])
    for files in sets:
        cmd = ["docker", "compose"]
        cmd += [arg for f in files for arg in ("-f", f)]
        if (root / ".env").exists():
            cmd += ["--env-file", str(root / ".env")]
        cmd += ["cp", f"zitadel:{container_path}", "-"]
        try:
            r = subprocess.run(cmd, cwd=str(root), capture_output=True, timeout=30)
        except Exception:
            continue
        if r.returncode != 0:
            continue
        try:
            tf = tarfile.open(fileobj=io.BytesIO(r.stdout))
            for member in tf.getmembers():
                if member.isfile():
                    return tf.extractfile(member).read().decode().strip()
        except tarfile.TarError:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Zitadel Management API (machine PAT)
# ─────────────────────────────────────────────────────────────────────────────

def mgmt(session, idp_base, pat, method, path, payload=None):
    return session.request(
        method,
        idp_base + path,
        json=payload if payload is not None else None,
        headers={"Authorization": f"Bearer {pat}"},
        timeout=T,
    )


def bootstrap_app(session, idp_base, pat, callback):
    """Create (or reuse) a throw-away PKCE OIDC app WITH the refresh grant.
    Returns (client_id, project_id, app_id) for cleanup."""
    r = mgmt(session, idp_base, pat, "POST", "/management/v1/projects/_search", {})
    if r.status_code != 200:
        raise E2EError(f"project search failed: HTTP {r.status_code}")
    pid = next(
        (p["id"] for p in r.json().get("result", []) if p.get("name") == APP_PROJECT),
        None,
    )
    if pid is None:
        r = mgmt(session, idp_base, pat, "POST", "/management/v1/projects",
                 {"name": APP_PROJECT})
        if r.status_code != 200:
            raise E2EError(f"project add failed: HTTP {r.status_code} {r.text[:120]}")
        pid = r.json().get("id")
    else:
        # Leftover from a crashed earlier run: remove stale apps so app-add
        # can't 409 (an old app's client id would mismatch our redirect).
        r = mgmt(session, idp_base, pat, "POST",
                 f"/management/v1/projects/{pid}/apps/_search", {})
        if r.status_code == 200:
            for app in r.json().get("result", []):
                if app.get("name") == APP_NAME:
                    mgmt(session, idp_base, pat, "DELETE",
                         f"/management/v1/projects/{pid}/apps/{app['id']}")
    body = {
        "name": APP_NAME,
        "redirectUris": [callback],
        "postLogoutRedirectUris": [callback],
        "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
        # S2 needs the refresh grant; the auth-code grant is the base flow.
        "grantTypes": [
            "OIDC_GRANT_TYPE_AUTHORIZATION_CODE",
            "OIDC_GRANT_TYPE_REFRESH_TOKEN",
        ],
        "appType": "OIDC_APP_TYPE_WEB",
        "authMethodType": "OIDC_AUTH_METHOD_TYPE_NONE",  # public client, PKCE only
        "devMode": True,  # relax redirect-uri scheme checks (local domains)
    }
    r = mgmt(session, idp_base, pat, "POST",
             f"/management/v1/projects/{pid}/apps/oidc", body)
    if r.status_code != 200:
        raise E2EError(f"oidc app add failed: HTTP {r.status_code} {r.text[:120]}")
    data = r.json()
    return data.get("clientId"), pid, data.get("appId")


def cleanup_app(session, idp_base, pat, pid, app_id):
    if app_id:
        mgmt(session, idp_base, pat, "DELETE",
             f"/management/v1/projects/{pid}/apps/{app_id}")
    if pid:
        mgmt(session, idp_base, pat, "DELETE", f"/management/v1/projects/{pid}")


# ─────────────────────────────────────────────────────────────────────────────
# Identity: provision a dedicated throw-away user (self-contained — no
# admin password needed, only the seeded machine PAT)
# ─────────────────────────────────────────────────────────────────────────────

def purge_user(session, idp_base, pat, uname):
    H = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}
    lst = session.post(idp_base + "/v2/users", headers=H, json={"queries": [
        {"userNameQuery": {"userName": uname, "method": "TEXT_QUERY_METHOD_EQUALS"}}
    ]}, timeout=T).json()
    for stale in (lst.get("result") or []):
        session.delete(idp_base + f"/v2/users/{stale.get('userId')}",
                       headers=H, timeout=T)


def provision_user(session, idp_base, pat, uname):
    """Create an identity WITH its initial password. Returns (userId, pw)."""
    pw = "E2e-" + secrets.token_urlsafe(9) + "!7"
    H = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}
    j = session.post(idp_base + "/v2/users/human", headers=H, json={
        "username": uname,
        "profile": {"givenName": "E2", "familyName": "Throwaway"},
        "email": {"email": f"{uname}@e2e.invalid", "isVerified": True},
        "password": {"password": pw, "changeRequired": False},
    }, timeout=T).json()
    return j.get("userId", ""), pw


# ─────────────────────────────────────────────────────────────────────────────
# v2 Session API login (what the Login v2 UI does)
# ─────────────────────────────────────────────────────────────────────────────

def lc_headers(lc_pat, user_id):
    return {
        "Authorization": f"Bearer {lc_pat}",
        "x-zitadel-login-client": user_id,
    }


def resolve_user_id(session, idp_base, auth_value, login_name):
    r = session.post(
        f"{idp_base}/management/v1/users/_search",
        json={"userName": login_name.split("@")[0]},
        headers={"Authorization": f"Bearer {auth_value}"},
        timeout=T,
    )
    if r.status_code != 200:
        return None
    stem = login_name.split("@")[0].lower()
    for u in r.json().get("result", []):
        if str(u.get("userName", "")).lower().split("@")[0] == stem:
            return str(u.get("id"))
    return None


def api_login(session, idp_base, lc_pat, client_uid, authz_url, user_id, pw):
    """Log the user in via the v2 Session API and finalize one auth request.
    Returns (callback_query, {"id": sessionId, "token": sessionToken})."""
    h = lc_headers(lc_pat, client_uid)

    r = session.get(authz_url, headers=h, allow_redirects=False, timeout=T)
    if r.status_code not in REDIRECTS or "Location" not in r.headers:
        raise E2EError(f"authorize probe: HTTP {r.status_code} {r.text[:80]}")
    m = re.search(r"authRequest=([A-Za-z0-9_:-]+)", r.headers["Location"])
    if not m:
        raise E2EError("authorize did not hand out an auth request id")
    req_id = m.group(1)

    r = session.post(
        f"{idp_base}/v2/sessions",
        json={"checks": {"user": {"userId": user_id}}},
        headers=h, timeout=T,
    )
    if r.status_code >= 300:
        raise E2EError(f"session create: HTTP {r.status_code} {r.text[:80]}")
    j = r.json()
    sid, stok = j.get("sessionId"), j.get("sessionToken")
    if not sid or not stok:
        raise E2EError("session create returned no id/token")

    r = session.patch(
        f"{idp_base}/v2/sessions/{sid}",
        json={"checks": {"password": {"password": pw}}},
        headers=h, timeout=T,
    )
    if r.status_code >= 300:
        raise E2EError(f"password check: HTTP {r.status_code} {r.text[:80]}")
    stok = r.json().get("sessionToken") or stok

    qs = finalize(session, idp_base, lc_pat, client_uid, req_id, sid, stok)
    return qs, {"id": sid, "token": stok}


def finalize(session, idp_base, lc_pat, client_uid, req_id, sid, stok):
    """Attach a session to an auth request; returns the callback query params.
    Raises E2EError when the IdP rejects the (session, auth request) pair."""
    r = session.post(
        f"{idp_base}/v2/oidc/auth_requests/{req_id}",
        json={"session": {"sessionId": sid, "sessionToken": stok}},
        headers=lc_headers(lc_pat, client_uid), timeout=T,
    )
    if r.status_code >= 300:
        raise E2EError(f"finalize auth request: HTTP {r.status_code} {r.text[:80]}")
    cb = r.json().get("callbackUrl", "")
    qs = parse_qs(urlparse(cb).query)
    if "code" not in qs:
        raise E2EError(f"callback without code: {sorted(qs)}")
    return qs


def exchange_code(session, disc, client_id, secret, code, verifier, callback, use_pkce=True):
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback,
        "client_id": client_id,
    }
    if use_pkce:
        data["code_verifier"] = verifier
    return session.post(
        disc["token_endpoint"],
        data=data,
        auth=(client_id, secret) if secret else None,
        timeout=T,
    )


# ─────────────────────────────────────────────────────────────────────────────
# S1 — Session timeout verification
# ─────────────────────────────────────────────────────────────────────────────

def section_timeout(result, session, disc, id_token, expires_in):
    now = int(time.time())

    # S1-1: credentials are time-bounded — the IdP hands out a finite,
    # positive lifetime for the access token.
    sane = (isinstance(expires_in, int) and 0 < expires_in <= MAX_SANE_LIFETIME_S)
    (result.ok if sane else result.fail)(
        f"S1 session timeout: tokens time-bounded (expires_in={expires_in}s)"
        if sane else f"S1 session timeout: expires_in={expires_in!r} not a sane positive lifetime"
    )

    # S1-2: the timeout claim is consistent end to end — the id_token JWT's
    # exp-iat matches the token response's expires_in (within skew) and the
    # window is currently valid (exp in the future, iat not from the future).
    try:
        h_b64, p_b64, s_b64 = id_token.split(".")
        _ = len(s_b64)  # 3-part JWS, not JWE
        claims = b64u_json(p_b64)
        exp, iat = claims.get("exp"), claims.get("iat")
    except (ValueError, json.JSONDecodeError) as e:
        result.fail(f"S1 session timeout: id_token not a decodable JWS ({e})")
        return

    lifetime = (exp - iat) if isinstance(exp, int) and isinstance(iat, int) else None
    consistent = (
        lifetime is not None
        and abs(lifetime - expires_in) <= SKEW_S
        and isinstance(exp, int) and exp > now
        and isinstance(iat, int) and iat <= now + SKEW_S
    )
    if consistent:
        result.ok(
            f"S1 session timeout: id_token exp-iat={lifetime}s matches "
            f"expires_in={expires_in}s and is currently valid"
        )
    else:
        detail = (f"exp-iat={lifetime}s expires_in={expires_in}s "
                  f"exp={exp} iat={iat} now={now}")
        result.fail(f"S1 session timeout: timeout claims inconsistent ({detail})")

    # S1-3: positive control — the credential is actually valid right now
    # (userinfo accepts the access token within its lifetime).



# ─────────────────────────────────────────────────────────────────────────────
# S2 — Token auto-refresh
# ─────────────────────────────────────────────────────────────────────────────

def section_refresh(result, session, disc, client_id, refresh_token,
                    expected_expires_in):
    # S2-2: the refresh grant must mint a NEW access token with a ROTATED
    # refresh token and a fresh lifetime — no re-login involved.
    r = session.post(
        disc["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        timeout=T,
    )
    if r.status_code != 200:
        result.fail(f"S2 token auto-refresh: refresh grant HTTP {r.status_code} "
                    f"{r.text[:120]}")
        return None

    body = r.json()
    new_access = body.get("access_token")
    rotated = body.get("refresh_token")
    fresh_lifetime = body.get("expires_in")

    ok_rotation = bool(new_access) and bool(rotated)
    if ok_rotation:
        result.ok("S2 token auto-refresh: refresh grant minted a new access token")
    else:
        result.fail("S2 token auto-refresh: refresh grant returned no access/"
                    "refresh token")
    if body.get("id_token"):
        result.ok("S2 token auto-refresh: fresh id_token issued with the refresh")
    else:
        result.warn("S2 token auto-refresh: no id_token in refresh response")

    sane_lifetime = (isinstance(fresh_lifetime, int) and 0 < fresh_lifetime)
    if fresh_lifetime == expected_expires_in:
        result.ok("S2 token auto-refresh: refreshed token got a fresh lifetime "
                  f"(expires_in={fresh_lifetime}s)")
    elif sane_lifetime:
        result.warn(f"S2 token auto-refresh: lifetime changed "
                    f"({expected_expires_in}s -> {fresh_lifetime}s)")
    else:
        result.fail(f"S2 token auto-refresh: refreshed expires_in={fresh_lifetime!r}")

    # S2-3: the refreshed token is LIVE — userinfo must accept it.
    if new_access:
        ui = session.get(
            disc.get("userinfo_endpoint", f"{disc['issuer']}/oidc/v1/userinfo"),
            headers={"Authorization": f"Bearer {new_access}"},
            timeout=T,
        )
        if ui.status_code == 200:
            result.ok("S2 token auto-refresh: refreshed access token is live "
                      "at userinfo")
        else:
            result.fail(f"S2 token auto-refresh: refreshed token rejected at "
                        f"userinfo (HTTP {ui.status_code})")
    return body


# ─────────────────────────────────────────────────────────────────────────────
# S3 — Cross-service SSO (portal → OpenCloud): one session, two RPs
# ─────────────────────────────────────────────────────────────────────────────

def section_cross_sso(result, session, disc, idp_base, lc_pat, client_uid,
                      sess, oc_client_id, oc_secret, cloud_base):
    oc_callback = f"{cloud_base}/oidc-callback.html"

    # S3-1: the OpenCloud OIDC client must be registered at the IdP,
    # otherwise cloud login is a dead end.
    r = session.get(
        disc["authorization_endpoint"],
        params={
            "client_id": oc_client_id,
            "response_type": "code",
            "scope": "openid",
            "nonce": "e2e-session-lifecycle",
            "redirect_uri": oc_callback,
        },
        allow_redirects=False, timeout=T,
    )
    if r.status_code not in REDIRECTS:
        if "App.NotFound" in r.text or "client" in r.text.lower():
            result.skip("S3 cross-service SSO: OpenCloud OIDC client not "
                        "registered in this subset (no cloud SSO to test)")
        else:
            result.fail(f"S3 cross-service SSO: OpenCloud authorize HTTP "
                        f"{r.status_code} {r.text[:80]}")
        return
    m = re.search(r"authRequest=([A-Za-z0-9_:-]+)", r.headers.get("Location", ""))
    if not m:
        result.fail("S3 cross-service SSO: OpenCloud authorize gave no auth request id")
        return
    result.ok("S3 cross-service SSO: OpenCloud OIDC client registered at the IdP")

    # S3-2: the portal's EXISTING IdP session finalizes OpenCloud's auth
    # request — a second service authorized without any credential check.
    try:
        qs = finalize(session, idp_base, lc_pat, client_uid, m.group(1),
                      sess["id"], sess["token"])
        result.ok("S3 cross-service SSO: OpenCloud authorize finalized with the "
                  "portal session — no second password check")
    except E2EError as e:
        result.fail(f"S3 cross-service SSO: shared session did not finalize "
                    f"OpenCloud auth request: {e}")
        return

    # S3-3: the OpenCloud code must be exchangeable for a real token that
    # the cloud would accept (best-effort: OpenCloud is a confidential
    # client, secret lives in .env; a minimal subset may not expose one).
    code = qs["code"][0]
    if not oc_secret or "CHANGEME" in oc_secret.upper():
        result.warn("S3 cross-service SSO: no OpenCloud client secret "
                    "(OC_OIDC_CLIENT_SECRET unset) — code exchange skipped")
        return
    r = session.post(
        disc["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": oc_callback,
            "client_id": oc_client_id,
        },
        auth=(oc_client_id, oc_secret),
        timeout=T,
    )
    if r.status_code != 200:
        result.warn(f"S3 cross-service SSO: OpenCloud code exchange HTTP "
                    f"{r.status_code} {r.text[:100]}")
        return
    access = r.json().get("access_token")
    ui = session.get(
        disc.get("userinfo_endpoint", f"{disc['issuer']}/oidc/v1/userinfo"),
        headers={"Authorization": f"Bearer {access}"},
        timeout=T,
    )
    if access and ui.status_code == 200:
        result.ok("S3 cross-service SSO: OpenCloud code exchange ok — cloud "
                  "token accepted at userinfo")
    else:
        result.warn("S3 cross-service SSO: OpenCloud token exchange incomplete "
                    f"(userinfo HTTP {ui.status_code})")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> bool:
    global VERIFY
    ap = argparse.ArgumentParser(description="openSME e2e session lifecycle")
    ap.add_argument("domain", nargs="?", default=None, help="stack domain")
    args = ap.parse_args()

    dotenv = load_env_file()

    def cfg(key, default=""):
        return os.environ.get(key) or dotenv.get(key) or default

    domain = (args.domain or cfg("OPENSME_DOMAIN", "localhost")).strip()
    localish = is_localish(domain)
    VERIFY = not (cfg("E2E_INSECURE") in ("1", "true", "yes") or localish)
    if not VERIFY:
        urllib3.disable_warnings()
    if localish:
        install_dns_fallback()

    schema = "https://" if VERIFY else "http://"
    idp_base = f"{schema}{cfg('ZITADEL_DOMAIN', f'auth.{domain}')}"
    portal_base = (
        "http://127.0.0.1:8080" if domain in ("localhost", "127.0.0.1")
        else f"{schema}{cfg('PORTAL_DOMAIN', f'portal.{domain}')}"
    )
    cloud_base = f"{schema}{cfg('OPENCLOUD_DOMAIN', f'cloud.{domain}')}"

    session = requests.Session()
    session.verify = VERIFY

    result = Result("session_lifecycle")
    result.header(f"openSME e2e session lifecycle — domain={domain} "
                  f"idp={idp_base} verify={VERIFY}")

    def reachable(url):
        try:
            session.get(url, timeout=5, allow_redirects=True)
            return True
        except requests.RequestException:
            return False

    if not reachable(idp_base + "/debug/ready"):
        result.fail("stack unreachable — start it first (./scripts/demo.sh or make up)")
        return result.summary()

    # OIDC discovery (also the issuer for all later checks)
    try:
        disc = session.get(idp_base + "/.well-known/openid-configuration",
                           timeout=T).json()
    except (requests.RequestException, ValueError) as e:
        result.fail(f"IdP discovery unreachable: {e.__class__.__name__}")
        return result.summary()
    result.ok("OIDC discovery ok")

    # Seeded machine credentials
    pat = cfg("E2E_ZITADEL_PAT") or fetch_seeded("/machinekey/pat")
    lc_pat = cfg("E2E_ZITADEL_LOGINCLIENT_PAT") or fetch_seeded("/machinekey/login-client.pat")
    if not pat:
        result.skip("no machine PAT reachable (docker compose down?) — "
                    "skipping session lifecycle")
        return result.summary()

    # Throw-away app + identity (owned, cleaned up in finally)
    callback = f"{idp_base}/session-lifecycle-cb"
    client_id, pid, app_id = None, None, None
    uid, pw = None, None

    try:
        try:
            client_id, pid, app_id = bootstrap_app(session, idp_base, pat, callback)
        except E2EError as e:
            result.fail(f"OIDC app bootstrap failed: {e}")
            return result.summary()

        if lc_pat:
            client_uid = resolve_user_id(session, idp_base, lc_pat, LOGIN_CLIENT_USER)
        else:
            client_uid = None
        if not lc_pat or not client_uid:
            result.skip("no login-client PAT (re-run `zitadel setup --steps "
                        "/steps.yaml` on a fresh DB)")
            return result.summary()

        purge_user(session, idp_base, pat, SESSION_USER)
        uid, pw = provision_user(session, idp_base, pat, SESSION_USER)
        if not uid:
            result.fail("test identity provisioning failed: POST /v2/users/human "
                        "returned no userId")
            return result.summary()
        result.ok(f"provisioned dedicated test identity {SESSION_USER}")

        # ── Login + first token exchange (offline_access → refresh_token) ──
        authz, verifier = authorize_url(disc, client_id, callback)
        qs, sess = api_login(session, idp_base, lc_pat, client_uid, authz, uid, pw)
        result.ok("login flow produced an authorization code")

        r = exchange_code(session, disc, client_id, None, qs["code"][0],
                          verifier, callback, use_pkce=True)
        if r.status_code != 200:
            result.fail(f"token exchange: HTTP {r.status_code} {r.text[:120]}")
            return result.summary()
        body = r.json()
        access = body.get("access_token")
        id_token = body.get("id_token")
        refresh_token = body.get("refresh_token")
        expires_in = body.get("expires_in")
        if not access or not id_token:
            result.fail("token response missing access/id tokens")
            return result.summary()
        result.ok("PKCE token exchange ok")

        # ── S1: session timeout verification ──
        section_timeout(result, session, disc, id_token, expires_in)

        # ── S2: token auto-refresh ──
        if refresh_token:
            section_refresh(result, session, disc, client_id, refresh_token,
                            expires_in)
        else:
            result.fail("S2 token auto-refresh: first exchange returned no "
                        "refresh_token (client grant or scope missing "
                        "offline_access?)")

        # ── S3: cross-service SSO (portal → OpenCloud) ──
        section_cross_sso(result, session, disc, idp_base, lc_pat, client_uid,
                          sess,
                          cfg("OC_OIDC_CLIENT_ID", "opencloud"),
                          cfg("OC_OIDC_CLIENT_SECRET"),
                          cloud_base)

        # portal reachability sanity (the RP the user starts from)
        try:
            pr = session.get(portal_base + "/", timeout=T)
            if pr.status_code == 200:
                result.ok("portal reachable")
            else:
                result.warn(f"portal HTTP {pr.status_code}")
        except requests.RequestException:
            result.skip("portal not running (SSO session still verified at the IdP)")
    finally:
        # Owned resources: purge the identity and remove the throw-away app
        # even on failure (idempotent — safe to run on crashed leftovers).
        try:
            if uid and pat:
                mgmt(session, idp_base, pat, "DELETE", f"/v2/users/{uid}")
                result.ok("removed test identity")
        except Exception:
            result.warn("could not remove test identity")
        if pid and pat:
            try:
                cleanup_app(session, idp_base, pat, pid, app_id)
                result.ok("removed throw-away OIDC app")
            except Exception:
                result.warn("could not remove throw-away OIDC app "
                            f"(delete project '{APP_PROJECT}')")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
