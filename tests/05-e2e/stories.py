#!/usr/bin/env python3
"""
tests/05-e2e/stories.py — Layer 5: user-story journeys across the shipped epics.

Walks the business-facing user stories end to end over HTTPS against a
RUNNING stack: the portal catalog contract (tiles = membership content),
the video-meeting epic (lobby → room → signaling auth), the booking →
join-link interop, the edge-hardening shadow pattern, and a
catalog-driven sweep of every configured tile surface.

Epics / user stories (IDs appear in failure output):

  Epic P — Portal (signed-in catalog, anonymous public landing)
    P1  anonymous landing = public landing: login CTA + news, no tiles,
        no signed-in chrome (no logout, no app.js)          [contracts/tiles.yml]
    P2  anonymous /api/services is refused (401) — the tiles API is gated
    P3  /health answers {"status":"ok"}
    P4  no dashboard-shaped leak: /dashboard anonymous never renders tiles
    P5  signed-in: the landing renders the tile grid (data-pal anchors)
    P6  signed-in: the header carries the logout affordance
    P7  signed-in: /api/services lists the configured services (JSON)
    P8  catalog gating: Meet tile present iff VC_URL is configured,
        href exactly VC_URL

  Epic V — Video meetings (vc-room + LiveKit)
    V1  lobby serves the join form
    V2  hostile room names are rejected (traversal, space, unicode, junk)
    V3  malformed display name → 400
    V4  room page embeds a bare-wss SERVER_URL (no /rtc suffix — the
        double-/rtc regression) and a 3-segment JWT
    V5  livekit-client SDK is vendored locally (no CDN), size sanity
    V6  /healthz answers ok
    V7  vc hosts lift the camera/mic policy; calendar keeps the blanket
        deny (edge isolation contract)
    V8  signaling auth: a minted HS256 video-grant token validates (200),
        garbage is rejected (401)  [skips without LIVEKIT_API_KEY/SECRET]

  Epic C — Booking calendar → join link (interop)
    C1  the public booking page is reachable anonymously
    C2  the booking page carries the VC join origin
    C3  the join origin equals the configured VC origin (single source
        of truth across epics)

  Epic K — Calendar SSO (calrs × Zitadel OIDC)
    K1  the calrs login page offers the SSO affordance (OIDC enabled
        in auth_config; the demo contract — local stacks without SSO skip)
    K2  /auth/oidc/login redirects to the IdP authorize endpoint with
        client_id + PKCE + state (discovery + client wiring live —
        this is the container-egress host-gateway contract)
    K3  redirect_uri == CALENDAR_URL + /auth/oidc/callback (no drift)
    K4  the OIDC handshake cookies are HttpOnly + Secure + SameSite
    K5  callback state gate: bogus state is refused (CSRF contract)

  Epic L — Member apps (intercom / polls / cloud picker)
    L1  member APIs refuse anonymous reads and writes
    L2  intercom signed-in CRUD roundtrip (post → list → delete)
    L3  intercom attachment host-pin: foreign attachment_url → 400
        "attachment host not allowed" (SSRF pinning)
    L4  covered by L1 (polls gate checked alongside intercom's)
    L5  polls validation matrix: 1 option → 400, 11 options → 400,
        vote option out of range → 400 (negative = raw 422 quirk,
        warned not failed)
    L6  polls signed-in create + vote + list roundtrip (no delete
        route — polls persist by design)
    L7  cloud share: anonymous refused; traversal ("../") → 400
        "refusing"; absolute path → 400 "must be relative"
    L8  announcements + feeds are anonymous-readable JSON envelopes

  Epic M — Session lifecycle & observability
    M1  signed-in logout → 303 to IdP end_session with
        post_logout_redirect_uri, clears the session cookies
        (Max-Age=0) and actually terminates the portal session
    M2  /metrics answers anonymously in Prometheus text format and
        leaks no secrets (no password/token/cookie material)
    M3  IdP pinning: Zitadel app client_id matches PORTAL_OIDC_CLIENT_ID
        and the app's redirect URI is the portal callback (PAT-gated)
    M4  invoiceninja IdP pinning: the redirect_uri the app SERVES in its
        /auth/oidc 302 is exactly what the Zitadel app REGISTERS (this
        drift broke invoice SSO: registered invoices.<OPENSME_DOMAIN>,
        served invoices.<INVOICE_DOMAIN>)                        [PAT-gated]

  Epic N — Notes (lasuite impress)
    N1  Next SPA shell served anonymously
    N2  API index JSON (/api/v1.0/) advertises documents/users URLs
    N3  anonymous mutation refused (POST documents → 401), anon list
        readable-but-empty (200)
    N4  whoami refuses anonymous (401)

  Epic W — Workflow (operaton BPM)
    W1  anon / → 302 /operaton/app/
    W2  full anonymous SSO chain: /operaton/app/ →
        /oauth2/authorization/operaton → IdP authorize → login UI
    W3  app-tier hardening on the BPM landing (nosniff+HSTS, XFO exempt)

  Epic D — Surveys (limesurvey)
    D1  public landing served (LimeSurvey)
    D2  /admin gated behind a login redirect
    D3  app-tier hardening on the landing

  Epic O — OpenCloud (file cloud)
    O1  SPA served anonymously
    O2  OCS capabilities API refuses anonymous (401)
    O3  app-tier hardening on the SPA

  Epic X — Webmail (SOGo6)
    X1  legacy /SOGo → 302 to the SPA root (retired URL contract)
    X2  locale landing /en served
    X3  app-tier hardening on the SPA
    X4  testuser's CalDAV subscription to the calrs booking store
        (external calendar, source_type=caldav → calendar.<host>/dav/booking/main/);
        edge answers PROPFIND 207 with booking auth, 401 anonymous, the
        collection carries a VCALENDAR resourcetype (regression gate for the
        empty-store bug) and a live mirror sync completes
    X5  full public-booking → CalDAV write-back → sogo6 mirror round trip
        (self-cleaning, unique slot/guest per run): the public book page
        confirms a booking, its VEVENT lands in the Radicale collection,
        the sogo6 mirror syncs it, dashboard-cancel DELETEs it from Radicale
        and a re-sync drops it from the mirror

  Epic A — Archive (paperless-ngx)
    A1  anon / → 302 /accounts/login/
    A2  login page carries the Zitadel OIDC affordance
    A3  documents API refuses anonymous (401)
    A4  app-tier hardening on the login page

  Epic G — Chat & Matrix (element + synapse)
    G1  Element SPA served on CHAT_URL
    G2  .well-known/matrix/client advertises the homeserver
    G3  synapse client versions endpoint answers JSON
    G4  app-tier hardening on the Element SPA

  Epic I — IdP traits (zitadel)
    I1  discovery document: issuer == IdP base, endpoints present
    I2  JWKS serves signing keys
    I3  token endpoint rejects a garbage grant with 400 (no 5xx)
    I4  bare end_session answers a graceful logout redirect
    I5  login UI served anonymously

  Epic Y — AI gating (portal)
    Y1  /api/ai/chat route absent while AI is unconfigured (404);
        warn-not-fail when an AI backend is configured

  Epic J — Signed-in SSO journeys (headless, real code exchange)
    J1  invoiceninja: testuser OIDC round trip → linked account (200;
        unlinked identities get the explicit invite-first 400)
    J2  paperless: allauth confirm → authorize → login → dashboard;
        OIDC user can read /api/documents/ (default-group perms)
    J3  operaton: webapp → oauth2 → IdP → signed welcome page
    J4  synapse: SSO redirect → login → "Continue to your account"
        loginToken handoff page

  Epic T — Mail (stalwart)
    T1  web UI served anonymously
    T2  app-tier hardening on the web UI

  Portal deepening:
    P12  session cookie hardening (HttpOnly, SameSite, Secure on https)
    P13  announcements readable anonymously (list contract)
    P14  feeds columns shape (server-side RSS aggregation)

    P9   /api/session anonymous introspection ({"authenticated": false})
    P10  /api/auth/login → 303 IdP authorize carrying the portal
         client_id + PKCE(S256) + state
    P11  /api/auth/callback refuses a bogus state (400 CSRF gate)

  Member-app deepening:
    L12  intercom upload round trip (raw-body endpoint → served file,
         traversal-guard 404s on hostile token/name)
    L13  polls upload round trip (query-metadata raw-body → served
         file, hostile name guard)

    L9   intercom validation matrix (empty text → 400, >2000 bytes →
         400, unknown kind → 400, unknown delete → 404, missing text
         → clean 400 after the serde-default fix)
    L10  intercom ?since= delta polling contract
    L11  polls revote → 409 conflict; foreign attachment_url → 400
         host-pin

  Epic E — Edge hardening (security-headers shadow contract)
    E1  portal: HSTS + XFO + nosniff
    E2  vc host: HSTS + XFO + nosniff (the camera/mic lift is V7)
    E3  calendar: HSTS + XFO + nosniff (the blanket camera deny is V7)
    E4  chat + surveys: HSTS + XFO + nosniff

  Epic S — Catalog surfaces (contracts/tiles.yml driven)
    S*  every CONFIGURED catalog tile: reachable anonymously (never 5xx)
        and hardened (XFO + HSTS on every routed first-party host)

Config (env > .env > default): OPENSME_DOMAIN, PORTAL_DOMAIN, VC_URL,
VC_DOMAIN, CALENDAR_URL, E2E_CALRS_USER, E2E_CALRS_EVENT,
LIVEKIT_API_KEY, LIVEKIT_API_SECRET, ZITADEL_ADMIN_PASSWORD,
E2E_ZITADEL_PAT, E2E_ZITADEL_LOGINCLIENT_PAT, PORTAL_OIDC_CLIENT_ID,
CALRS_ADMIN_EMAIL, CALRS_ADMIN_PASSWORD.

Usage:
  python3 tests/05-e2e/stories.py                  # domain from .env
  python3 tests/05-e2e/stories.py opensme.local

Exit codes: 0 = pass, 1 = failures (skips never fail).
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.parse
import urllib3
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import ROOT, Result

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import (
    T,
    api_login,
    install_dns_fallback,
    is_localish,
    load_env_file,
    portal_login,
    probe,
    resolve_user_id,
)

CATALOG = ROOT / "contracts" / "tiles.yml"


def capture_authorize(sess, url, post=None):
    """Walk an app's redirect chain, return the /oauth/v2/authorize URL.

    `post` is (data, action) for form-initiated flows (django-allauth).
    """
    if post is not None:
        data, action = post
        r = sess.post(action or url, data=data, timeout=T, allow_redirects=False)
    else:
        r = sess.get(url, timeout=T, allow_redirects=False)
    loc = r.headers.get("location", "")
    for _ in range(8):
        if not loc.startswith("http"):
            return ""
        if "/oauth/v2/authorize" in loc:
            return loc
        loc = sess.get(loc, timeout=T, allow_redirects=False).headers.get(
            "location", loc)
    return ""


def oidc_login(sess, idp_base, authz_url, user_uid, pw, lc_pat):
    """Complete the two-step Zitadel login and return the callback response.

    Drives run.api_login (v2 Session API + finalize) against the app's
    authorize URL, then GETs the app's redirect_uri with the minted code.
    """
    qs, _ = api_login(sess, idp_base, lc_pat, user_uid, authz_url, user_uid, pw)
    ru = urllib.parse.parse_qs(urllib.parse.urlparse(authz_url).query).get(
        "redirect_uri", [""])[0]
    return sess.get(ru + "?" + urllib.parse.urlencode(qs, doseq=True),
                    timeout=T, allow_redirects=True)


def resolve_routable(sess, candidates):
    """First candidate that actually routes (probe /), else None."""
    for cand in candidates:
        if cand and probe(sess, cand.rstrip("/") + "/"):
            return cand
    return None


_LC_PAT_CACHE: dict = {}


def _lc_pat(sess):
    """Resolve the login-client PAT (env first, seeded container fallback)."""
    pat = os.environ.get("E2E_ZITADEL_LOGINCLIENT_PAT")
    if pat:
        return pat
    if "v" not in _LC_PAT_CACHE:
        from run import fetch_seeded
        _LC_PAT_CACHE["v"] = fetch_seeded("/machinekey/login-client.pat")
    return _LC_PAT_CACHE["v"]


def mint_livekit_token(api_key: str, api_secret: str, room: str, identity: str,
                       ttl_s: int = 300) -> str:
    """HS256 LiveKit access token (stdlib only — no pyjwt dependency).

    Same claim shape vc-room mints: iss=api key, video grant roomJoin.
    """
    now = int(time.time())

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({
        "iss": api_key,
        "sub": identity,
        "iat": now,
        "exp": now + ttl_s,
        "video": {"roomJoin": True, "room": room,
                  "canPublish": True, "canSubscribe": True,
                  "canPublishData": True},
    }).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = b64(hmac.new(api_secret.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def parse_server_url(html: str):
    """Extract SERVER_URL from a vc-room page (JSON-encoded string)."""
    m = re.search(r'const SERVER_URL = ("(?:[^"\\]|\\.)*")', html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def hardened(resp: requests.Response, result: Result, label: str, hsts: bool = True,
             xfo_required: bool = True):
    """security-headers shadow contract: XFO + nosniff (+ HSTS) present.

    xfo_required=False for app-tier surfaces: the *_sec file routers
    (app-hardening middleware) deliberately ship only HSTS+nosniff — the
    apps frame themselves (Invoice Ninja PDF preview, Paperless viewer)
    and carry their own (SAMEORIGIN) X-Frame-Options where they need one.
    """
    xfo = resp.headers.get("x-frame-options", "")
    nosniff = resp.headers.get("x-content-type-options", "")
    if xfo.upper() in ("DENY", "SAMEORIGIN"):
        result.ok(f"{label}: X-Frame-Options {xfo}")
    elif xfo_required:
        result.fail(f"{label}: X-Frame-Options missing/weak ({xfo!r})")
    else:
        result.warn(f"{label}: no X-Frame-Options (app-tier — apps set their own)")
    if nosniff.lower() == "nosniff":
        result.ok(f"{label}: X-Content-Type-Options nosniff")
    else:
        result.fail(f"{label}: X-Content-Type-Options missing ({nosniff!r})")
    if not hsts:
        return
    sts = resp.headers.get("strict-transport-security", "")
    if "max-age" in sts:
        result.ok(f"{label}: Strict-Transport-Security {sts}")
    else:
        result.fail(f"{label}: Strict-Transport-Security missing")


def reachable(session, url, timeout=T):
    """Probe a surface; returns (ok, response-or-None). Never raises."""
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        return r.status_code < 500, r
    except requests.RequestException:
        return False, None


def main() -> bool:
    ap = argparse.ArgumentParser(description="openSME user-story journeys (epics)")
    ap.add_argument("domain", nargs="?", default=None, help="stack domain")
    args = ap.parse_args()

    dotenv = load_env_file()

    def cfg(key, default=""):
        return os.environ.get(key) or dotenv.get(key) or default

    domain = (args.domain or cfg("OPENSME_DOMAIN", "localhost")).strip()
    localish = is_localish(domain)
    if localish:
        install_dns_fallback()
    urllib3.disable_warnings() if localish else None
    verify = not localish
    schema = "http://" if localish else "https://"

    portal_host = cfg("PORTAL_DOMAIN", f"home.{domain}")
    if localish:
        portal_base = "http://127.0.0.1:8080"
    else:
        portal_base = f"{schema}{portal_host}"
    idp_base = f"{schema}{cfg('ZITADEL_DOMAIN', f'auth.{domain}')}"

    vc_url = cfg("VC_URL") or (
        f"{schema}{cfg('VC_DOMAIN', '')}" if cfg("VC_DOMAIN") else "")
    cal_url = cfg("CALENDAR_URL") or (f"{schema}calendar.{portal_host}"
                                      if portal_host else "")
    calrs_user = cfg("E2E_CALRS_USER", "demo")
    calrs_event = cfg("E2E_CALRS_EVENT", "vc")

    session = requests.Session()
    session.verify = verify

    result = Result("stories")
    result.header(f"user stories across epics — domain={domain} portal={portal_base}")

    # ── stack reachability gate ──────────────────────────────────────────
    ok, _ = reachable(session, portal_base + "/health", timeout=10)
    if not ok:
        result.fail("portal unreachable — start the stack first "
                    "(scripts/demo.sh / make up), or fix PORTAL_DOMAIN")
        return result.summary()

    # ══ Epic P — Portal ═══════════════════════════════════════════════
    result.header("Epic P — Portal: signed-in catalog, anonymous public landing")

    anon = requests.Session()  # deliberately cookie-free
    anon.verify = verify
    r = anon.get(portal_base + "/", timeout=T)
    if r.status_code != 200:
        result.fail(f"P1: anonymous / answered {r.status_code}, want 200")
    else:
        html = r.text
        leaks = []
        if 'action="/api/logout"' in html or "Abmelden" in html:
            leaks.append("logout affordance")
        if "data-pal=" in html:
            leaks.append("tile grid (data-pal)")
        if "/app.js" in html:
            leaks.append("app.js (signed-in client script)")
        if 'href="/api/auth/login"' in html and "Anmelden" in html and not leaks:
            result.ok("P1: anonymous landing = public landing (CTA, no signed-in chrome)")
        else:
            result.fail(f"P1: anonymous landing leaks signed-in UI: {leaks or 'no login CTA'}")

    r = anon.get(portal_base + "/api/services", timeout=T, allow_redirects=False)
    if r.status_code in (401, 403):
        result.ok(f"P2: /api/services refuses anonymous ({r.status_code})")
    else:
        result.fail(f"P2: /api/services answered {r.status_code}, want 401/403")

    r = anon.get(portal_base + "/health", timeout=T)
    body = r.text
    if r.status_code == 200 and '"ok"' in body:
        result.ok("P3: /health ok")
    else:
        result.fail(f"P3: /health → {r.status_code} {body[:40]!r}")

    r = anon.get(portal_base + "/dashboard", timeout=T, allow_redirects=False)
    if r.status_code != 200 or "data-pal=" not in r.text:
        result.ok("P4: no /dashboard tile leak (404/redirect)")
    else:
        result.fail("P4: /dashboard renders tiles anonymously")

    r = anon.get(portal_base + "/api/session", timeout=T)
    if r.status_code == 200 and (r.json() or {}).get("authenticated") is False:
        result.ok("P9: /api/session anonymous introspection "
                  '({"authenticated": false})')
    else:
        result.fail(f"P9: /api/session → {r.status_code} {r.text[:60]!r}, "
                    "want 200 authenticated:false")

    # the portal is a CONFIDENTIAL client: plain authorization-code flow
    # with client secret (no PKCE) — pin client_id + state instead
    r = anon.get(portal_base + "/api/auth/login", timeout=T, allow_redirects=False)
    loc = r.headers.get("location", "")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    want_client = cfg("PORTAL_OIDC_CLIENT_ID")
    client_ok = (not want_client) or q.get("client_id", [""])[0] == want_client
    scope_ok = "openid" in (q.get("scope", [""])[0])
    if (r.status_code in (303, 302) and f"{idp_base}/oauth/v2/authorize" in loc
            and q.get("response_type") == ["code"]
            and q.get("state")
            and client_ok and scope_ok):
        result.ok("P10: /api/auth/login → 303 IdP authorize "
                  "(client_id, state, openid scope; confidential client)")
    else:
        result.fail(f"P10: auth/login → {r.status_code} {loc[:70]!r} "
                    f"(client match: {client_ok}, scope: {scope_ok})")

    r = anon.get(portal_base + "/api/auth/callback?code=e2e-bogus&state=e2e-bogus",
                 timeout=T, allow_redirects=False)
    if r.status_code == 400:
        result.ok("P11: callback refuses bogus state (CSRF gate)")
    else:
        result.fail(f"P11: bogus callback accepted ({r.status_code})")

    # session cookie hardening: capture the raw Set-Cookie the portal
    # hands out during a REAL login (hooks fire on every response)
    captured: dict = {}

    def _sc_hook(resp, **_kw):
        sc = resp.headers.get("set-cookie", "")
        if "OPENSMEPORTALSESSION" in sc:
            captured["sc"] = sc
        return resp

    ps = requests.Session()
    ps.verify = verify
    ps.hooks["response"] = [_sc_hook]
    if not portal_login(ps, portal_base, idp_base):
        result.warn("P12: portal_login failed — cookie flags unverified")
    else:
        sc = captured.get("sc", "")
        flags = {
            "HttpOnly": "httponly" in sc.lower(),
            "SameSite": "samesite" in sc.lower(),
            "Secure": "secure" in sc.lower(),
        }
        if flags["HttpOnly"] and flags["SameSite"] and (flags["Secure"] or localish):
            result.ok(f"P12: session cookie flags HttpOnly+SameSite"
                      f"{'+Secure' if flags['Secure'] else ''}")
        else:
            result.fail(f"P12: session cookie flags missing: {flags}")
        r = ps.get(portal_base + "/api/session", timeout=T)
        sess_body = r.json() or {}
        if r.status_code == 200 and sess_body.get("authenticated") is True:
            result.ok("P12: signed /api/session introspection "
                      f"(name={sess_body.get('name')!r})")
        else:
            result.warn(f"P12: signed session introspection → {r.status_code}")
    ps.hooks["response"] = []

    r = anon.get(portal_base + "/api/announcements", timeout=T)
    body = r.json() if r.headers.get("content-type", "").startswith(
        "application/json") else {}
    if r.status_code == 200 and isinstance(body.get("announcements"), list):
        result.ok("P13: announcements readable anonymously (list contract)")
    else:
        result.fail(f"P13: announcements → {r.status_code} {r.text[:50]!r}")
    r = anon.get(portal_base + "/api/feeds", timeout=T)
    try:
        cols = (r.json() or {}).get("columns") or []
        entry_keys = sorted((cols[0].get("entries") or [{}])[0]) if cols else []
    except ValueError:
        cols, entry_keys = [], []
    if r.status_code == 200 and isinstance(cols, list) \
            and (not cols or {"source", "title"} <= set(entry_keys)):
        result.ok(f"P14: feeds columns shape ({len(cols)} columns)")
    else:
        result.fail(f"P14: feeds → {r.status_code} "
                    f"{entry_keys or 'no columns'}")

    # ── signed-in journeys ────────────────────────────────────────────
    signed = requests.Session()
    signed.verify = verify
    if portal_login(signed, portal_base, idp_base):
        r = signed.get(portal_base + "/", timeout=T)
        html = r.text
        tiles = re.findall(r'data-pal="([^"]+)"', html)
        if len(tiles) >= 5:
            result.ok(f"P5: signed-in landing renders {len(tiles)} tiles {tiles}")
        else:
            result.fail(f"P5: signed-in landing shows {len(tiles)} tiles, want ≥5")
        if "Abmelden" in html:
            result.ok("P6: signed-in header carries logout affordance")
        else:
            result.fail("P6: signed-in landing has no logout affordance")
        r = signed.get(portal_base + "/api/services", timeout=T)
        svc_ids = []
        if r.status_code == 200:
            try:
                svc_ids = [s.get("id") for s in r.json().get("services", [])]
            except ValueError:
                pass
        if svc_ids:
            result.ok(f"P7: /api/services lists {len(svc_ids)} services {svc_ids}")
        else:
            result.fail(f"P7: /api/services → {r.status_code}, no service list")
        # P8: catalog gating — Meet tile iff VC_URL configured, href exact
        if vc_url:
            if "vc" in svc_ids:
                r = signed.get(portal_base + "/", timeout=T)
                hrefs = re.findall(r'<a href="([^"]+)" class="card func-card"[^>]*data-pal="Meet"', r.text)
                if hrefs and hrefs[0].rstrip("/") == vc_url.rstrip("/"):
                    result.ok("P8: Meet tile gated in, href == VC_URL")
                else:
                    result.fail(f"P8: Meet tile href mismatch {hrefs!r} vs {vc_url!r}")
            else:
                result.fail("P8: VC_URL configured but no Meet tile in /api/services")
        else:
            if "vc" in svc_ids:
                result.fail("P8: VC_URL unset but Meet tile rendered (gating broken)")
            else:
                result.ok("P8: Meet tile correctly absent without VC_URL")
    else:
        result.skip("P5–P8: no session (set ZITADEL_ADMIN_PASSWORD + "
                    "E2E_ZITADEL_LOGINCLIENT_PAT / E2E_ZITADEL_PAT)")

    # ══ Epic V — Video meetings ═══════════════════════════════════════
    result.header("Epic V — Video meetings (vc-room + LiveKit)")
    if not vc_url:
        result.skip("V1–V8: VC not configured (VC_URL empty)")
    else:
        ok, r = reachable(session, vc_url + "/")
        if not ok:
            result.fail(f"V1: lobby unreachable ({getattr(r, 'status_code', 'conn')})")
        else:
            if 'id="room"' in r.text:
                result.ok("V1: lobby serves the join form")
            else:
                result.fail("V1: lobby has no join form")
            hardened(r, result, "V1 lobby")

        hostile = ["/room/../etc", "/room/has%20space", "/room/room%2Fx",
                   "/room/" + "a" * 65, "/room/r%C3%A4um", "/room/-leading"]
        bad = [u for u in hostile if session.get(vc_url + u, timeout=T).status_code != 404]
        if not bad:
            result.ok("V2: hostile room names rejected (404)")
        else:
            result.fail(f"V2: room names not rejected: {bad}")

        r = session.get(vc_url + "/room/e2e-story?name=<script>alert(1)</script>",
                        timeout=T)
        if r.status_code == 400:
            result.ok("V3: malformed display name → 400")
        else:
            result.fail(f"V3: malformed display name → {r.status_code}, want 400")

        r = session.get(vc_url + "/room/e2e-story?name=Alice", timeout=T)
        if r.status_code != 200:
            result.fail(f"V4: valid room → {r.status_code}, want 200")
        else:
            server_url = parse_server_url(r.text)
            tok = re.search(r'const TOKEN = "([^"]+)"', r.text)
            jwt_ok = bool(tok and tok.group(1).count(".") == 2)
            if server_url is None:
                result.fail("V4: room page embeds no SERVER_URL")
            elif server_url.rstrip("/").endswith("/rtc"):
                result.fail(f"V4: SERVER_URL carries /rtc suffix ({server_url!r}) — "
                            "the SDK appends /rtc itself (double-path regression)")
            elif not server_url.startswith(("wss://", "ws://")):
                result.fail(f"V4: SERVER_URL not a websocket origin ({server_url!r})")
            elif not jwt_ok:
                result.fail("V4: room page embeds no 3-segment JWT token")
            else:
                result.ok(f"V4: bare-wss SERVER_URL ({server_url}) + JWT token")
            hardened(r, result, "V4 room")

            pp = r.headers.get("permissions-policy", "")
            if "camera=(self)" in pp and "microphone=(self)" in pp:
                result.ok("V7: room lifts camera/mic policy (=(self))")
            else:
                result.fail(f"V7: room Permissions-Policy blocks camera/mic ({pp!r})")

        r = session.get(vc_url + "/static/livekit-client.umd.min.js", timeout=T)
        if (r.status_code == 200 and len(r.content) > 100_000
                and "javascript" in r.headers.get("content-type", "")):
            result.ok(f"V5: SDK vendored locally ({len(r.content)//1024} KB)")
        else:
            result.fail(f"V5: SDK not vendored ({r.status_code}, "
                        f"{len(r.content)} B, {r.headers.get('content-type')!r})")

        r = session.get(vc_url + "/healthz", timeout=T)
        if r.status_code == 200 and "ok" in r.text:
            result.ok("V6: healthz ok")
        else:
            result.fail(f"V6: healthz → {r.status_code} {r.text[:20]!r}")

        # edge isolation: another first-party host keeps the blanket deny
        if cal_url:
            r = session.get(cal_url + "/", timeout=T)
            pp = r.headers.get("permissions-policy", "")
            if "camera=()" in pp and "microphone=()" in pp:
                result.ok("V7: calendar keeps blanket camera/microphone deny")
            else:
                result.warn(f"V7: calendar Permissions-Policy not blanket ({pp!r})")

        api_key, api_secret = cfg("LIVEKIT_API_KEY"), cfg("LIVEKIT_API_SECRET")
        if api_key and api_secret:
            tok = mint_livekit_token(api_key, api_secret, "e2e-story", "e2e-story-probe")
            r = session.get(vc_url + f"/rtc/validate?access_token={tok}", timeout=T)
            if r.status_code == 200:
                result.ok("V8: minted video-grant token validates (200)")
            else:
                result.fail(f"V8: valid token rejected ({r.status_code})")
            r = session.get(vc_url + "/rtc/validate?access_token=garbage", timeout=T)
            if r.status_code in (401, 403):
                result.ok(f"V8: garbage token rejected ({r.status_code})")
            else:
                result.fail(f"V8: garbage token accepted ({r.status_code})")
        else:
            result.skip("V8: LIVEKIT_API_KEY/SECRET not in env — "
                        "signaling auth roundtrip not exercised")

    # ══ Epic C — Booking → join link ══════════════════════════════════
    result.header("Epic C — Booking calendar → join link")
    if not cal_url:
        result.skip("C1–C3: CALENDAR_URL not configured")
    else:
        booking = f"{cal_url.rstrip('/')}/u/{calrs_user}/{calrs_event}"
        ok, r = reachable(session, booking)
        if not ok:
            result.fail(f"C1: booking page unreachable ({getattr(r, 'status_code', 'conn')}): {booking}")
        else:
            result.ok(f"C1: booking page public ({r.status_code})")
            hardened(r, result, "C1 booking")
            m = re.search(r"https://(vc\.[A-Za-z0-9.-]+)", r.text)
            if not m:
                result.fail("C2: booking page carries no VC join link")
            else:
                join_origin = f"{schema}{m.group(1)}"
                result.ok(f"C2: booking page references VC origin ({join_origin})")
                if vc_url and join_origin.rstrip("/") == vc_url.rstrip("/"):
                    result.ok("C3: join origin == configured VC origin")
                else:
                    result.fail(f"C3: join origin {join_origin!r} != VC_URL "
                                f"{vc_url!r} — drift between booking and VC")

    # ══ Epic K — Calendar SSO (calrs × Zitadel OIDC) ══════════════════
    result.header("Epic K — Calendar SSO (calrs × Zitadel OIDC)")
    if not cal_url:
        result.skip("K1–K5: CALENDAR_URL not configured")
    else:
        r = anon.get(cal_url + "/auth/login", timeout=T)
        sso_on = r.status_code == 200 and "auth/oidc/login" in r.text
        if not sso_on:
            if localish:
                result.skip("K1–K5: calrs OIDC not enabled on this stack "
                            "(enable via dashboard → admin → OIDC)")
            else:
                result.fail("K1: calrs login page offers no SSO affordance "
                            "— OIDC disabled in auth_config (demo contract "
                            "requires SSO; enable via dashboard admin or "
                            "POST /dashboard/admin/oidc)")
        else:
            result.ok("K1: calrs login page offers the SSO affordance")

            r = anon.get(cal_url + "/auth/oidc/login", timeout=T, allow_redirects=False)
            loc = r.headers.get("location", "")
            if r.status_code != 303 or f"{idp_base}/oauth/v2/authorize" not in loc:
                result.fail(f"K2: /auth/oidc/login → {r.status_code} "
                            f"{loc[:60]!r} (want 303 → IdP authorize; "
                            "discovery needs the host-gateway extra_host)")
            else:
                result.ok("K2: /auth/oidc/login → 303 IdP authorize")
                q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
                cid, redirect = q.get("client_id", [""])[0], q.get("redirect_uri", [""])[0]
                has_pkce = bool(q.get("code_challenge")) and q.get("code_challenge_method", [""])[0] == "S256"
                has_state = bool(q.get("state"))
                if has_pkce and has_state and cid:
                    result.ok(f"K2: authorize carries client_id={cid[:8]}…, PKCE(S256), state")
                else:
                    result.fail(f"K2: authorize params incomplete "
                                f"(pkce={has_pkce}, state={has_state}, cid={bool(cid)})")
                want_redirect = cal_url.rstrip("/") + "/auth/oidc/callback"
                if redirect.rstrip("/") == want_redirect:
                    result.ok("K3: redirect_uri == CALENDAR_URL + /auth/oidc/callback")
                else:
                    result.fail(f"K3: redirect_uri {redirect!r} != {want_redirect!r}")

                cookie_names = {c.name for c in anon.cookies if c.name.startswith("calrs_oidc_")}
                flags = r.headers.get("set-cookie", "")
                if ({"calrs_oidc_state", "calrs_oidc_nonce", "calrs_oidc_pkce"} <= cookie_names
                        and "httponly" in flags.lower() and "samesite=lax" in flags.lower()):
                    result.ok(f"K4: handshake cookies minted "
                              f"({sorted(cookie_names)}), HttpOnly+SameSite=Lax")
                else:
                    result.warn(f"K4: handshake cookie contract not fully asserted "
                                f"(names={sorted(cookie_names)})")

                r = anon.get(cal_url + "/auth/oidc/callback?code=e2e-bogus&state=e2e-bogus",
                             timeout=T)
                body = r.text[:120].lower()
                if "state" in body:
                    result.ok("K5: callback refuses bogus state (CSRF gate)")
                else:
                    result.fail(f"K5: bogus-state callback accepted?! {body!r}")

    # ══ Epic L — Member apps (intercom / polls / cloud picker) ════════
    result.header("Epic L — Member apps (intercom / polls / cloud picker)")

    # ── anonymous gates ──────────────────────────────────────────────
    gates = []
    for label, url in (("intercom", portal_base + "/api/intercom"),
                       ("polls", portal_base + "/api/polls"),
                       ("cloud-share", portal_base + "/api/cloud/share?path=x.txt")):
        r = anon.get(url, timeout=T, allow_redirects=False)
        r2 = anon.post(url, json={"x": 1}, timeout=T, allow_redirects=False)
        ok = r.status_code in (401, 403, 303) and r2.status_code in (401, 403, 303)
        gates.append(f"{label} {r.status_code}/{r2.status_code}{'✓' if ok else '✗'}")
    if all("✓" in g for g in gates):
        result.ok(f"L1/L4/L7: member APIs refuse anonymous ({'; '.join(gates)})")
    else:
        result.fail(f"L1/L4/L7: anonymous gate broken: {'; '.join(gates)}")

    r = anon.get(portal_base + "/api/announcements", timeout=T)
    r2 = anon.get(portal_base + "/api/feeds", timeout=T)
    try:
        ann_ok = r.status_code == 200 and "announcements" in r.json()
        feeds_ok = r2.status_code == 200 and "columns" in r2.json()
    except ValueError:
        ann_ok = feeds_ok = False
    if ann_ok and feeds_ok:
        result.ok("L8: announcements + feeds anonymous JSON envelopes (200)")
    else:
        result.fail(f"L8: announcements {r.status_code}/feeds {r2.status_code} "
                    "anonymous envelope broken")

    # ── signed-in member-app journeys ────────────────────────────────
    member = requests.Session()
    member.verify = verify
    if portal_login(member, portal_base, idp_base):
        # intercom CRUD roundtrip (serde ignores unknown fields by design)
        r = member.post(portal_base + "/api/intercom",
                        json={"text": "e2e-story roundtrip",
                              "bogus_unknown_field": 1}, timeout=T)
        mid = None
        if r.status_code == 201:
            msg = r.json().get("message") or {}
            mid = msg.get("id")
        if mid is not None:
            result.ok("L2: intercom post created (201, serde-tolerant payload)")
            r = member.get(portal_base + "/api/intercom", timeout=T)
            listed = any(str(m.get("id")) == str(mid)
                         for m in (r.json().get("messages") or []))
            if listed:
                result.ok("L2: intercom list carries the new message")
            else:
                result.fail("L2: intercom list missing the created message")
            r = member.delete(portal_base + f"/api/intercom/{mid}", timeout=T)
            if r.status_code < 400:
                result.ok("L2: intercom delete cleans up")
            else:
                result.fail(f"L2: intercom delete → {r.status_code}")
        else:
            result.fail(f"L2: intercom post → {r.status_code} {r.text[:60]!r}")

        r = member.post(portal_base + "/api/intercom",
                        json={"text": "e2e", "kind": "note",
                              "attachment_url": "https://evil.example.com/x.png"},
                        timeout=T)
        if r.status_code == 400 and "host not allowed" in r.text:
            result.ok("L3: intercom attachment host-pin (foreign host → 400)")
        else:
            result.fail(f"L3: intercom foreign attachment → {r.status_code}, "
                        "want 400 'host not allowed' (SSRF pinning)")

        # polls create validation matrix
        matrix = []
        for label, payload in (("empty question", {"question": "", "options": ["a", "b"]}),
                               ("one option", {"question": "q", "options": ["only"]}),
                               ("eleven options", {"question": "q",
                                                   "options": [f"o{i}" for i in range(11)]})):
            r = member.post(portal_base + "/api/polls", json=payload, timeout=T)
            ok = r.status_code == 400
            matrix.append(f"{label}→{r.status_code}{'✓' if ok else '✗'}")
        if all("✓" in m for m in matrix):
            result.ok(f"L5: polls create validation {', '.join(matrix)}")
        else:
            result.fail(f"L5: polls create validation broken: {', '.join(matrix)}")

        # polls roundtrip + vote range
        r = member.post(portal_base + "/api/polls",
                        json={"question": "e2e-story poll?", "options": ["yes", "no"]},
                        timeout=T)
        pid = None
        if r.status_code == 201:
            pid = (r.json().get("poll") or {}).get("id")
        if pid is not None:
            result.ok("L6: polls create")
            # vote range probes FIRST — a real vote latches the
            # already-voted 409 guard ahead of the option check
            r = member.post(portal_base + f"/api/polls/{pid}/vote", json={"option": 99},
                            timeout=T)
            if r.status_code == 400:
                result.ok("L5: polls vote option=99 → 400")
            else:
                result.fail(f"L5: polls vote option=99 → {r.status_code}, want 400")
            # negative option: i64 extractor + handler guard → clean
            # JSON 400 (was a raw 422 usize rejection before the fix)
            r = member.post(portal_base + f"/api/polls/{pid}/vote", json={"option": -1},
                            timeout=T)
            if r.status_code == 400:
                result.ok("L5: polls option=-1 → clean 400 (serde-fix)")
            elif r.status_code == 422:
                result.warn("L5: polls option=-1 → raw 422 (pre-fix portal image)")
            else:
                result.warn(f"L5: polls option=-1 → {r.status_code} (quirk moved?)")
            r = member.post(portal_base + f"/api/polls/{pid}/vote", json={"option": 1},
                            timeout=T)
            if r.status_code == 200:
                result.ok("L6: polls vote accepted")
            else:
                result.fail(f"L6: polls vote → {r.status_code}")
            r = member.get(portal_base + "/api/polls", timeout=T)
            listed = any(str(p.get("id")) == str(pid)
                         for p in (r.json().get("polls") or []))
            if listed:
                result.ok("L6: polls list carries the new poll")
            else:
                result.fail("L6: polls list missing the created poll")
        else:
            result.fail(f"L6: polls create → {r.status_code} {r.text[:60]!r}")

        # cloud share path validation
        bad_paths = []
        for label, path, marker in (("traversal", "../etc/passwd", "refusing"),
                                    ("absolute", "/abs/path", "must be relative"),
                                    ("dot-dot", "a/../../b", "refusing")):
            r = member.post(portal_base + f"/api/cloud/share?path={urllib.parse.quote(path)}",
                            timeout=T)
            ok = r.status_code == 400 and marker in r.text
            bad_paths.append(f"{label}→{r.status_code}{'✓' if ok else '✗'}")
        if all("✓" in m for m in bad_paths):
            result.ok(f"L7: cloud share path validation {', '.join(bad_paths)}")
        else:
            result.fail(f"L7: cloud share path validation broken: {', '.join(bad_paths)}")

        # intercom validation matrix
        checks = []
        r = member.post(portal_base + "/api/intercom", json={"text": ""}, timeout=T)
        checks.append(f"empty→{r.status_code}"
                      + ("✓" if r.status_code == 400 and "empty" in r.text else "✗"))
        r = member.post(portal_base + "/api/intercom",
                        json={"text": "x" * 5000}, timeout=T)
        checks.append(f"oversize→{r.status_code}"
                      + ("✓" if r.status_code == 400 and "2000" in r.text else "✗"))
        r = member.post(portal_base + "/api/intercom",
                        json={"text": "probe", "kind": "weird-kind"}, timeout=T)
        checks.append(f"bad-kind→{r.status_code}"
                      + ("✓" if r.status_code == 400 else "✗"))
        r = member.post(portal_base + "/api/intercom", json={}, timeout=T)
        # serde default: missing text now reaches the clean 400, not a raw 422
        checks.append(f"no-text→{r.status_code}"
                      + ("✓" if r.status_code == 400 else "✗"))
        r = member.delete(portal_base + "/api/intercom/999999999", timeout=T)
        checks.append(f"del-unknown→{r.status_code}"
                      + ("✓" if r.status_code == 404 else "✗"))
        if all("✓" in c for c in checks):
            result.ok(f"L9: intercom validation {', '.join(checks)}")
        else:
            result.fail(f"L9: intercom validation broken: {', '.join(checks)}")

        # since-delta polling: create one message, ?since=<id> must see none
        r = member.post(portal_base + "/api/intercom", json={"text": "delta-probe"},
                        timeout=T)
        did = (r.json().get("message") or {}).get("id")
        if did is not None:
            r = member.get(portal_base + f"/api/intercom?since={did}", timeout=T)
            body = r.json() or {}
            if r.status_code == 200 and not body.get("messages"):
                result.ok("L10: intercom ?since= delta returns no-newer")
            else:
                result.fail(f"L10: since={did} → {r.status_code} "
                            f"{len(body.get('messages') or [])} newer")
            member.delete(portal_base + f"/api/intercom/{did}", timeout=T)
        else:
            result.fail(f"L10: intercom create for delta → {r.status_code}")

        # polls revote conflict + foreign attachment host-pin
        r = member.post(portal_base + "/api/polls",
                        json={"question": "revote?", "options": ["a", "b"]},
                        timeout=T)
        rpid = (r.json().get("poll") or {}).get("id")
        if rpid is not None:
            member.post(portal_base + f"/api/polls/{rpid}/vote",
                        json={"option": 0}, timeout=T)
            r = member.post(portal_base + f"/api/polls/{rpid}/vote",
                            json={"option": 1}, timeout=T)
            if r.status_code == 409 and "already voted" in r.text:
                result.ok("L11: polls revote → 409 conflict")
            else:
                result.fail(f"L11: polls revote → {r.status_code}, want 409")
        r = member.post(portal_base + "/api/polls",
                        json={"question": "q", "options": ["a", "b"],
                              "attachment_url": "https://evil.example.com/f"},
                        timeout=T)
        if r.status_code == 400 and "host not allowed" in r.text:
            result.ok("L11: polls attachment host-pin (foreign host → 400)")
        else:
            result.fail(f"L11: polls foreign attachment → {r.status_code}, "
                        "want 400 'host not allowed'")

        r = member.post(portal_base + "/api/polls",
                        json={"question": "q", "options": ["a", "b"],
                              "attachment_url": "https://evil.example.com/f"},
                        timeout=T)
        if r.status_code == 400 and "host not allowed" in r.text:
            result.ok("L11: polls attachment host-pin (foreign host → 400)")
        else:
            result.fail(f"L11: polls foreign attachment → {r.status_code}, "
                        "want 400 'host not allowed'")

        # intercom upload round trip: raw-body endpoint, metadata in query
        r = member.post(portal_base + "/api/intercom/upload"
                        "?text=upload-probe&kind=note&filename=e2e-note.txt",
                        data=b"e2e attachment bytes", timeout=T)
        aurl = ((r.json().get("message") or {}).get("attachment") or {}).get("url") \
            if r.status_code == 201 else ""
        full = aurl if aurl.startswith("http") else portal_base + aurl
        served = member.get(full, timeout=T) if aurl else None
        if r.status_code == 201 and served is not None and served.status_code == 200 \
                and served.content == b"e2e attachment bytes":
            result.ok(f"L12: intercom upload round trip ({aurl[:44]}…)")
            member.delete(portal_base
                          + f"/api/intercom/{(r.json().get('message') or {}).get('id')}",
                          timeout=T)
        else:
            result.fail(f"L12: intercom upload → {r.status_code}, served "
                        f"{getattr(served, 'status_code', '-')}")
        # hostile file paths must not traverse (guards: hex token +
        # sanitize-unchanged name)
        hostile_files = [
            "/api/intercom/files/zzzz/a.txt",
            "/api/intercom/files/0123456789abcdef/../../etc/passwd",
            "/api/intercom/files/0123456789abcdef/..%2f..%2fetc%2fpasswd",
        ]
        bad = [u for u in hostile_files
               if member.get(portal_base + u, timeout=T).status_code != 404]
        if not bad:
            result.ok("L12: intercom file guards reject hostile token/name")
        else:
            result.fail(f"L12: file traversal leak on {bad}")

        # polls upload round trip (query-metadata raw-body endpoint)
        opts = urllib.parse.quote(json.dumps(["a", "b"]))
        r = member.post(portal_base + "/api/polls/upload"
                        f"?question=e2e-upload&options={opts}&filename=e2e.csv",
                        data=b"a,b\n1,2", timeout=T)
        poll = (r.json() or {}).get("poll") or {}
        aurl = (poll.get("attachment") or {}).get("url") \
            if r.status_code == 201 else ""
        # with a cloud config the attachment lives behind an ABSOLUTE
        # OpenCloud share URL; without one it's the local /api/files/… path
        full = aurl if aurl.startswith("http") else portal_base + aurl
        served = member.get(full, timeout=T) if aurl else None
        if r.status_code == 201 and served is not None and served.status_code == 200:
            result.ok(f"L13: polls upload round trip ({aurl[:44]}…)")
            r = member.post(portal_base + f"/api/polls/{poll.get('id')}/vote",
                            json={"option": 0}, timeout=T)
            ok_vote = r.status_code == 200
        else:
            ok_vote = False
            result.fail(f"L13: polls upload → {r.status_code}, served "
                        f"{getattr(served, 'status_code', '-')}")
        if ok_vote:
            r = member.get(portal_base + "/api/files/zzzz/not-here.csv",
                           timeout=T)
            if r.status_code == 404:
                result.ok("L13: polls file guard rejects hostile name (404)")
            else:
                result.fail(f"L13: polls file guard → {r.status_code}, want 404")
    else:
        result.skip("L2–L7 signed-in member journeys: no session "
                    "(set ZITADEL_ADMIN_PASSWORD + E2E_ZITADEL_LOGINCLIENT_PAT / "
                    "E2E_ZITADEL_PAT)")

    # ══ Epic M — Session lifecycle & observability ════════════════════
    result.header("Epic M — Session lifecycle & observability")

    if portal_login(member, portal_base, idp_base):
        r = member.post(portal_base + "/api/logout", timeout=T, allow_redirects=False)
        loc = r.headers.get("location", "")
        cleared = r.headers.get("set-cookie", "").lower()
        if r.status_code == 303 and f"{idp_base}/oidc/v1/end_session" in loc \
                and "post_logout_redirect_uri=" in loc:
            result.ok("M1: logout 303 → IdP end_session + post_logout_redirect_uri")
        else:
            result.fail(f"M1: logout → {r.status_code} {loc[:70]!r}")
        if "max-age=0" in cleared and "httponly" in cleared:
            result.ok("M1: logout clears session cookies (Max-Age=0, HttpOnly)")
        else:
            result.fail("M1: logout does not clear session cookies")
        r = member.get(portal_base + "/api/services", timeout=T, allow_redirects=False)
        if r.status_code in (401, 403):
            result.ok("M1: session actually terminated after logout")
        else:
            result.fail(f"M1: /api/services still answers after logout ({r.status_code})")
    else:
        result.skip("M1: no session (portal login unavailable)")

    r = anon.get(portal_base + "/metrics", timeout=T)
    if r.status_code == 200 and ("# HELP" in r.text or "# TYPE" in r.text
                                 or "http_requests" in r.text):
        # scan with route TEMPLATE labels neutralized: metric routes like
        # /api/files/:token/:name legitimately contain marker words —
        # what must never leak are VALUES (passwords, raw session ids)
        leak_scan = re.sub(r'(route|path)="[^"]*"', '\1="X"',
                           r.text.lower())
        leak_markers = [m for m in ("password", "secret", "token", "set-cookie",
                                    "session=") if m in leak_scan]
        if leak_markers:
            result.fail(f"M2: /metrics leaks sensitive markers: {leak_markers}")
        else:
            result.ok("M2: /metrics anonymous Prometheus body, no secret markers")
        hardened(r, result, "M2 metrics", xfo_required=False)
    else:
        result.fail(f"M2: /metrics → {r.status_code}, want 200 prometheus text")

    pat = cfg("E2E_ZITADEL_PAT")
    portal_client = cfg("PORTAL_OIDC_CLIENT_ID")
    if not (pat and portal_client):
        result.skip("M3: IdP pinning needs E2E_ZITADEL_PAT + PORTAL_OIDC_CLIENT_ID")
    else:
        h = {"Authorization": f"Bearer {pat}"}
        r = requests.post(f"{idp_base}/management/v1/projects/_search", headers=h,
                          json={}, timeout=T, verify=verify)
        pinned, detail = False, ""
        if r.status_code == 200:
            for proj in r.json().get("result", []):
                ra = requests.post(
                    f"{idp_base}/management/v1/projects/{proj['id']}/apps/_search",
                    headers=h, json={}, timeout=T, verify=verify)
                for app in ra.json().get("result", []):
                    oc = app.get("oidcConfig") or {}
                    if oc.get("clientId") == portal_client:
                        uris = oc.get("redirectUris") or []
                        want = f"{portal_base}/api/auth/callback"
                        if any(u.rstrip("/") == want.rstrip("/") for u in uris):
                            pinned = True
                            detail = f"client {portal_client[:8]}… redirect == {want}"
                        else:
                            detail = f"client {portal_client[:8]}… redirect {uris!r} != {want!r}"
        if pinned:
            result.ok(f"M3: IdP pinning verified ({detail})")
        else:
            result.fail(f"M3: IdP pinning broken ({detail or r.status_code})")

    ininja_client = cfg("INVOICENINJA_OIDC_CLIENT_ID")
    invoice_host = cfg("INVOICE_DOMAIN") or f"invoices.{portal_host}"
    invoice_base = f"{schema}{invoice_host}"
    if not ininja_client:
        result.skip("M4: invoiceninja IdP pinning needs INVOICENINJA_OIDC_CLIENT_ID")
    else:
        # the SERVED half: what redirect_uri the app puts on its authorize
        # bounce (derived from the APP_URL the deployment actually runs)
        served = None
        r = anon.get(invoice_base + "/auth/oidc", timeout=T, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(
                r.headers.get("location", "")).query)
            served = q.get("redirect_uri", [None])[0]
        if not pat or not served:
            result.skip("M4: served redirect unverifiable "
                        f"(served={served!r}, PAT={'yes' if pat else 'no'})")
        else:
            want_served = invoice_base + "/auth/oidc"
            served_ok = served.rstrip("/") == want_served.rstrip("/")
            # the REGISTERED half: the IdP app must allow exactly that URI
            registered = None
            if pat:
                h = {"Authorization": f"Bearer {pat}"}
                r = requests.post(f"{idp_base}/management/v1/projects/_search",
                                  headers=h, json={}, timeout=T, verify=verify)
                if r.status_code == 200:
                    for proj in r.json().get("result", []):
                        ra = requests.post(
                            f"{idp_base}/management/v1/projects/{proj['id']}/apps/_search",
                            headers=h, json={}, timeout=T, verify=verify)
                        for app in ra.json().get("result", []):
                            if (app.get("oidcConfig") or {}).get("clientId") == ininja_client:
                                registered = app["oidcConfig"].get("redirectUris") or []
            reg_ok = registered is not None and any(
                u.rstrip("/") == served.rstrip("/") for u in registered)
            if served_ok and reg_ok:
                result.ok("M4: invoiceninja IdP pinning verified "
                          f"(served == registered == {served})")
            elif not served_ok:
                result.fail(f"M4: invoiceninja serves redirect {served!r}, "
                            f"want {want_served!r} (APP_URL/routing drift)")
            else:
                result.fail(f"M4: invoiceninja redirect drift — served {served!r} "
                            f"but IdP registers {registered!r} "
                            "(fix: PUT /management/v1/projects/<pid>/apps/<aid>/"
                            "oidc_config with the served URI — see "
                            "scripts/zitadel-client.sh enforce_redirect_uris)")

    # ══ Epic N — Notes (lasuite impress) ══════════════════════════════
    result.header("Epic N — Notes (lasuite impress)")
    notes_host = cfg("NOTES_DOMAIN") or f"notes.{portal_host}"
    if is_localish(notes_host):
        result.skip("N: notes host localish")
    else:
        ok, _ = reachable(anon, f"{schema}{notes_host}/")
        if not ok:
            result.skip(f"N: notes not deployed ({notes_host})")
        else:
            r = anon.get(f"{schema}{notes_host}/", timeout=T)
            if r.status_code == 200 and "_next/static" in r.text:
                result.ok("N1: impress SPA shell served anonymously")
            else:
                result.fail(f"N1: notes / → {r.status_code}, want 200 SPA shell")
            r = anon.get(f"{schema}{notes_host}/api/v1.0/", timeout=T)
            try:
                api = r.json()
                docs = (api.get("documents") or "")
                api_ok = r.status_code == 200 and notes_host in docs
            except ValueError:
                api_ok, docs = False, ""
            if api_ok:
                result.ok(f"N2: API index advertises documents URL ({docs[:52]}…)")
            else:
                result.fail(f"N2: API index → {r.status_code} {docs[:60]!r}")
            r = anon.post(f"{schema}{notes_host}/api/v1.0/documents/", json={},
                          timeout=T)
            lst = anon.get(f"{schema}{notes_host}/api/v1.0/documents/", timeout=T)
            if r.status_code == 401:
                result.ok("N3: anonymous document mutation refused (401)")
            else:
                result.fail(f"N3: anonymous document POST → {r.status_code}, want 401")
            if lst.status_code == 200 and (lst.json() or {}).get("count") == 0:
                result.ok("N3: anonymous list readable-but-empty (count=0)")
            else:
                result.warn(f"N3: anonymous list → {lst.status_code} "
                            f"{lst.text[:60]!r} (upstream contract moved?)")
            r = anon.get(f"{schema}{notes_host}/api/v1.0/users/whoami/", timeout=T)
            if r.status_code == 401:
                result.ok("N4: whoami refuses anonymous (401)")
            else:
                result.fail(f"N4: whoami → {r.status_code}, want 401")

    # ══ Epic W — Workflow (operaton BPM) ══════════════════════════════
    result.header("Epic W — Workflow (operaton BPM)")
    bpm_url = cfg("BPM_URL") or (f"{schema}bpm.{portal_host}" if portal_host else "")
    if not bpm_url or is_localish(bpm_url.rsplit("//", 1)[-1].split("/")[0]):
        result.skip("W: BPM host localish")
    else:
        ok, _ = reachable(anon, bpm_url + "/")
        if not ok:
            result.skip(f"W: operaton not deployed ({bpm_url})")
        else:
            r = anon.get(bpm_url + "/", timeout=T, allow_redirects=False)
            if r.status_code == 302 and "/operaton/app" in r.headers.get("location", ""):
                result.ok("W1: anon / → 302 /operaton/app/")
            else:
                result.fail(f"W1: bpm / → {r.status_code} "
                            f"{r.headers.get('location', '')[:60]!r}")
            # the full anonymous SSO chain: webapp → spring oauth2 → IdP
            r = anon.get(bpm_url + "/operaton/app/", timeout=T, allow_redirects=False)
            hop2 = r.headers.get("location", "")
            chain_ok = r.status_code in (301, 302, 303, 307) \
                and "/oauth2/authorization/" in hop2
            hop3 = ""
            if chain_ok:
                r = anon.get(hop2, timeout=T, allow_redirects=False)
                hop3 = r.headers.get("location", "")
                chain_ok = r.status_code in (301, 302, 303, 307) \
                    and f"{idp_base}/oauth/v2/authorize" in hop3 \
                    and "client_id=" in hop3
            hop4_ok = False
            if chain_ok:
                r = anon.get(hop3, timeout=T, allow_redirects=False)
                hop4 = r.headers.get("location", "")
                hop4_ok = r.status_code in (301, 302, 303, 307) \
                    and "/ui/v2/login" in hop4
            if chain_ok and hop4_ok:
                result.ok("W2: anonymous SSO chain webapp→oauth2→authorize→login UI")
            else:
                result.fail(f"W2: SSO chain broken (hops: {hop2[:50]!r} "
                            f"→ {hop3[:50]!r})")
            hardened(anon.get(bpm_url + "/", timeout=T), result, "W3 bpm",
                     xfo_required=False)

    # ══ Epic D — Surveys (limesurvey) ═════════════════════════════════
    result.header("Epic D — Surveys (limesurvey)")
    surv_url = cfg("SURVEYS_URL") or (f"{schema}surveys.{portal_host}"
                                      if portal_host else "")
    if not surv_url or is_localish(surv_url.rsplit("//", 1)[-1].split("/")[0]):
        result.skip("D: surveys host localish")
    else:
        ok, _ = reachable(anon, surv_url + "/")
        if not ok:
            result.skip(f"D: limesurvey not deployed ({surv_url})")
        else:
            r = anon.get(surv_url + "/", timeout=T)
            if r.status_code == 200 and "LimeSurvey" in r.text:
                result.ok("D1: public landing served (LimeSurvey)")
            else:
                result.fail(f"D1: surveys / → {r.status_code}, want 200 LimeSurvey")
            # /admin canonicalizes (/admin → /admin/, may chain through
            # scheme rewrites) and lands on the LimeSurvey login page
            r = anon.get(surv_url + "/admin", timeout=T)
            if r.history and r.status_code == 200 and "authentication" in str(r.url):
                result.ok("D2: /admin chains to the login page "
                          f"({len(r.history)} redirects)")
            else:
                result.fail(f"D2: /admin → {r.status_code} {str(r.url)[:60]!r}, "
                            "want redirect chain to login")
            hardened(r, result, "D3 surveys", xfo_required=False)

    # ══ Epic O — OpenCloud ════════════════════════════════════════════
    result.header("Epic O — OpenCloud")
    conv_cloud = f"{schema}cloud.{portal_host}" if portal_host else ""
    env_cloud = cfg("OPENCLOUD_URL") or ((cfg("OPENCLOUD_DOMAIN") and
                                          schema + cfg("OPENCLOUD_DOMAIN")) or "")
    # operator shells often carry stale *_DOMAIN exports that beat .env —
    # probe env first, then the <svc>.<portal_host> convention; skip only
    # when neither routes
    cloud_url = ""
    for cand in (env_cloud, conv_cloud):
        if cand and not cloud_url:
            ok, _ = reachable(anon, cand + "/")
            if ok:
                cloud_url = cand
    if not cloud_url:
        result.skip(f"O: opencloud not routable ({env_cloud or conv_cloud!r})")
    else:
        r = anon.get(cloud_url + "/", timeout=T)
        if r.status_code == 200 and "<html" in r.text[:400].lower():
            result.ok(f"O1: SPA served anonymously ({cloud_url})")
        else:
            result.fail(f"O1: cloud / → {r.status_code}, want 200 SPA")
        r = anon.get(cloud_url + "/ocs/v2.php/cloud/capabilities", timeout=T)
        if r.status_code in (401, 403):
            result.ok(f"O2: OCS capabilities refuses anonymous ({r.status_code})")
        else:
            result.fail(f"O2: OCS capabilities → {r.status_code}, want 401/403")
        hardened(anon.get(cloud_url + "/", timeout=T), result, "O3 cloud",
                 xfo_required=False)

    # ══ Epic X — Webmail (SOGo6) ══════════════════════════════════════
    result.header("Epic X — Webmail (SOGo6)")
    conv_wm = f"{schema}webmail.{portal_host}" if portal_host else ""
    env_wm = cfg("SOGO_URL") or ((cfg("SOGO_DOMAIN") and
                                  schema + cfg("SOGO_DOMAIN")) or "")
    wm_url = ""
    for cand in (env_wm, conv_wm):
        if cand and not wm_url:
            ok, _ = reachable(anon, cand + "/")
            if ok:
                wm_url = cand
    if not wm_url:
        result.skip(f"X: webmail not routable ({env_wm or conv_wm!r})")
    else:
        r = anon.get(wm_url + "/SOGo", timeout=T, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307):
            result.ok(f"X1: legacy /SOGo → {r.status_code} "
                      f"{r.headers.get('location', '')[:40]!r} (retired URL)")
        else:
            result.fail(f"X1: /SOGo → {r.status_code}, want redirect to SPA root")
        r = anon.get(wm_url + "/en", timeout=T)
        if r.status_code == 200:
            result.ok("X2: locale landing /en served")
        else:
            result.fail(f"X2: /en → {r.status_code}, want 200")
        hardened(anon.get(wm_url + "/en", timeout=T), result, "X3 webmail",
                 xfo_required=False)

        # X4 — sogo6 external CalDAV subscription to calrs (provisioned by
        # scripts/provision-sogo-caldav.sh): the booking store is exposed
        # read-at-the-edge at calendar.<host>/dav/ (Radicale htpasswd on
        # top of the edge route) because sogo6's ICS fetcher is https-only
        # and rejects private IPs.
        cal_url = cfg("CALENDAR_URL") or (f"{schema}calendar.{portal_host}"
                                          if portal_host else "")
        rad_user, rad_pw = cfg("RADICALE_USER"), cfg("RADICALE_PASSWORD")
        t_uid = None
        try:
            t_uid = resolve_user_id(anon, idp_base,
                                    cfg("E2E_ZITADEL_PAT"), "testuser")
        except Exception:
            t_uid = None
        if not (cal_url and rad_user and rad_pw and t_uid
                and cfg("E2E_ZITADEL_LOGINCLIENT_PAT")):
            result.skip("X4: needs CALENDAR_URL + RADICALE_* + PAT env")
        else:
            ok, _ = reachable(anon, cal_url + "/")
            if not ok:
                result.skip(f"X4: calendar not routable ({cal_url})")
            else:
                dav = cal_url.rstrip("/") + "/dav/booking/"
                r = anon.get(dav, timeout=T, allow_redirects=False)
                # PROPFIND needs a raw method — use requests with method
                ra = requests.request("PROPFIND", dav, timeout=T,
                                      auth=(rad_user, rad_pw), verify=verify,
                                      headers={"Depth": "0"})
                rn = requests.request("PROPFIND", dav, timeout=T,
                                      verify=verify, headers={"Depth": "0"})
                if ra.status_code == 207 and rn.status_code == 401:
                    result.ok("X4: calrs CalDAV edge — PROPFIND 207 with "
                              "booking auth, 401 anonymous")
                else:
                    result.fail(f"X4: PROPFIND auth={ra.status_code} "
                                f"anon={rn.status_code}, want 207/401")
                # regression gate for the empty-booking-store bug: the
                # calendar collection must exist as a real VCALENDAR
                # (<C:calendar> in the resourcetype). A bare principal dir
                # (/booking/) returns 207 too but has no calendar tag, and
                # calrs v1.6.0 (pre-0002 patch) discovered ZERO calendars
                # from Radicale's spaced `<C:calendar />` resourcetype.
                main = cal_url.rstrip("/") + "/dav/booking/main"
                rc = requests.request("PROPFIND", main + "/", timeout=T,
                                      auth=(rad_user, rad_pw), verify=verify,
                                      headers={"Depth": "0"})
                if rc.status_code == 207 and "<C:calendar" in rc.text:
                    result.ok("X4: booking collection /dav/booking/main/ is "
                              "a VCALENDAR collection (PROPFIND 207 + tag)")
                else:
                    result.fail(f"X4: PROPFIND {main}/ → {rc.status_code}, "
                                f"calendar resourcetype missing — booking "
                                f"store not initialized (run "
                                f"scripts/calendar-setup.sh)")
                # the subscription on testuser's sogo6 account
                s = requests.Session()
                s.verify = verify
                rr = s.get(wm_url + "/api/user/v1/auth/mode",
                           params={"username": "testuser@opensme.local",
                                   "redirect": wm_url + "/"}, timeout=T)
                loc = ((rr.json().get("data") or {}).get("location") or "")
                if loc:
                    qs, _ = api_login(s, idp_base, cfg("E2E_ZITADEL_LOGINCLIENT_PAT"),
                                      t_uid, loc, t_uid,
                                      cfg("E2E_TESTUSER_PASSWORD", "eval2026"))
                    ru = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)[
                        "redirect_uri"][0]
                    rr = s.get(ru + "?" + urllib.parse.urlencode(qs, doseq=True),
                               timeout=T, allow_redirects=True)
                    tok = rr.url.split("#token=")[-1]
                    h = {"Authorization": f"Bearer {tok}"}
                    rr = s.get(wm_url + "/api/user/v1/calendars", headers=h, timeout=T)
                    ext = [c for c in ((rr.json().get("data") or {}).get("calendars", []))
                           if c.get("source_type") == "caldav"]
                    # the API serializers omit sync_config (credentials are
                    # never echoed) — the durable proof of the wiring is a
                    # live mirror sync against the booking collection:
                    # completed means sogo6 could fetch /dav/booking/main/
                    # with the stored credentials (the pre-fix empty store
                    # with no calendar collection mirrored nothing).
                    sub = ext[0] if ext else None
                    if sub and "calrs" in (sub.get("name") or "").lower():
                        key = sub.get("key") or ""
                        result.ok(f"X4: testuser sogo6 carries the calrs CalDAV "
                                  f"subscription ({sub['name']!r} → /dav/booking/main/)")
                        if key:
                            trig = s.post(wm_url + "/api/user/v1/external-calendars/"
                                          + key + "/sync", headers=h, timeout=T)
                            if trig.status_code == 202:
                                time.sleep(12)
                                st = s.get(wm_url + "/api/user/v1/external-calendars/"
                                           + key + "/sync", headers=h, timeout=T)
                                stjson = st.json().get("data") or {}
                                if stjson.get("sync_status") == "completed":
                                    result.ok("X4: sogo6 mirror sync of the calrs "
                                              "booking collection → completed")
                                else:
                                    result.fail("X4: sogo6 mirror sync → "
                                                f"{stjson.get('sync_status')} "
                                                f"({stjson.get('sync_error')})")
                            else:
                                result.warn(f"X4: sync trigger → {trig.status_code}")
                    else:
                        result.fail(f"X4: no calrs caldav subscription on testuser "
                                    f"(have {[c.get('name') for c in ext]})")
                else:
                    result.warn("X4: webmail login-mode discovery failed")

        # X5 — full public-booking → CalDAV write-back → sogo6 mirror round
        # trip, self-cleaning. Regression gate for the historical empty-store
        # bug: a booking made on the PUBLIC page must (1) land as a VEVENT in
        # the Radicale booking collection, (2) be picked up by testuser's
        # sogo6 mirror, and afterwards (3) cancelling it must DELETE the
        # event from Radicale and (4) drop it from the mirror. Uses a unique
        # far-future weekday slot + unique guest email per run and cancels
        # the booking at the end (dashboard flow, which is the path that
        # writes back the DELETE), so repeated runs never collide and leave
        # no residue.
        if not (cal_url and rad_user and rad_pw and t_uid
                and cfg("E2E_ZITADEL_LOGINCLIENT_PAT")
                and cfg("CALRS_ADMIN_EMAIL") and cfg("CALRS_ADMIN_PASSWORD")):
            result.skip("X5: needs CALENDAR_URL + RADICALE_* + PAT env "
                        "+ CALRS_ADMIN_EMAIL/PASSWORD (dashboard cancel)")
        else:
            x5_ok = True
            booked = None
            marker = f"x5.{int(time.time())}@example.org"
            try:
                # ── X5.1: book a free future weekday slot via the public page
                def _report_cal():
                    q = ('<?xml version="1.0"?>'
                         '<c:calendar-query xmlns:d="DAV:" '
                         'xmlns:c="urn:ietf:params:xml:ns:caldav">'
                         '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
                         '<c:filter><c:comp-filter name="VCALENDAR">'
                         '<c:comp-filter name="VEVENT"/></c:comp-filter>'
                         '</c:filter></c:calendar-query>')
                    return requests.request(
                        "REPORT", cal_url.rstrip("/") + "/dav/booking/main/",
                        timeout=T, auth=(rad_user, rad_pw), verify=verify,
                        headers={"Depth": "1"}, data=q)

                base = datetime.date.today() + datetime.timedelta(days=60)
                for _ in range(6):
                    d = base + datetime.timedelta(days=1)
                    while d.weekday() >= 5:   # keep Mon–Fri (availability)
                        d += datetime.timedelta(days=1)
                    base = d
                    s5 = requests.Session(); s5.verify = verify
                    rr = s5.get(f"{cal_url.rstrip('/')}/u/{calrs_user}/"
                                f"{calrs_event}/book?date={d}&time=09:30",
                                timeout=T)
                    cj = "; ".join(c.name + "=" + c.value for c in s5.cookies)
                    m = re.search(r"calrs_csrf=([^;]+)", cj)
                    tok = m.group(1) if m else ""
                    rr = s5.post(f"{cal_url.rstrip('/')}/u/{calrs_user}/"
                                 f"{calrs_event}/book",
                                 data={"_csrf": tok, "date": str(d),
                                       "time": "09:30", "tz": "Europe/Berlin",
                                       "name": "X5 Roundtrip",
                                       "email": marker, "notes": "x5 e2e"},
                                 timeout=T)
                    low = rr.text.lower()
                    if "no longer available" not in low and \
                       "too soon" not in low:
                        booked = (s5, d, marker, calrs_user, calrs_event)
                        break
                if not booked:
                    result.fail("X5: no free slot after 6 tries — booking "
                                "page refuses all candidates")
                    x5_ok = False
                else:
                    result.ok(f"X5: public booking confirmed on {booked[1]} "
                              f"(guest {marker})")

                    # ── X5.2: VEVENT landed in Radicale booking collection
                    rr = _report_cal()
                    if rr.status_code == 207 and marker in rr.text:
                        result.ok("X5: booking VEVENT present in Radicale "
                                  "/dav/booking/main/ (write-back landed)")
                    else:
                        result.fail(f"X5: write-back missing (REPORT "
                                    f"{rr.status_code}, marker absent) — the "
                                    f"empty-store regression")
                        x5_ok = False

                    # ── X5.3: sogo6 mirror picks the event up
                    s6 = requests.Session(); s6.verify = verify
                    rr = s6.get(wm_url + "/api/user/v1/auth/mode",
                                params={"username": "testuser@opensme.local",
                                        "redirect": wm_url + "/"}, timeout=T)
                    loc = ((rr.json().get("data") or {}).get("location") or "")
                    got_events = None
                    if loc:
                        qs, _ = api_login(s6, idp_base,
                                          cfg("E2E_ZITADEL_LOGINCLIENT_PAT"),
                                          t_uid, loc, t_uid,
                                          cfg("E2E_TESTUSER_PASSWORD", "eval2026"))
                        ru6 = urllib.parse.parse_qs(
                            urllib.parse.urlparse(loc).query)["redirect_uri"][0]
                        rr = s6.get(ru6 + "?" + urllib.parse.urlencode(qs, doseq=True),
                                    timeout=T, allow_redirects=True)
                        h6 = {"Authorization": "Bearer " +
                              rr.url.split("#token=")[-1]}
                        rr = s6.get(wm_url + "/api/user/v1/calendars",
                                    headers=h6, timeout=T)
                        sub6 = next((c for c in
                                     ((rr.json().get("data") or {})
                                      .get("calendars", []))
                                     if c.get("source_type") == "caldav"), None)
                        if sub6:
                            key6 = sub6.get("key") or ""
                            s6.post(wm_url + "/api/user/v1/external-calendars/"
                                    + key6 + "/sync", headers=h6, timeout=T)
                            for _ in range(10):
                                time.sleep(6)
                                st = s6.get(wm_url + "/api/user/v1/external-"
                                            "calendars/" + key6 + "/sync",
                                            headers=h6, timeout=T)
                                stj = st.json().get("data") or {}
                                if stj.get("sync_status") == "completed":
                                    break
                            lo = (booked[1] - datetime.timedelta(days=2)).isoformat()
                            hi = (booked[1] + datetime.timedelta(days=3)).isoformat()
                            ev = s6.get(wm_url + "/api/user/v1/calendars/"
                                        + key6 + "/events?start_date_time=" + lo
                                        + "&end_date_time=" + hi,
                                        headers=h6, timeout=T)
                            got_events = (ev, marker)
                            if marker in ev.text:
                                result.ok("X5: sogo6 mirror synced the booking "
                                          "event (visible via events API)")
                            else:
                                result.fail("X5: booking event not in the sogo6 "
                                            "mirror after sync completed")
                                x5_ok = False
                        else:
                            result.fail("X5: no caldav subscription on testuser — "
                                        "mirror cannot be verified")
                            x5_ok = False
                    else:
                        result.warn("X5: webmail login-mode discovery failed "
                                    "(mirror check skipped)")
            finally:
                # ── X5.4 (cleanup — ALWAYS): dashboard-cancel the booking,
                # which fires the CalDAV DELETE into Radicale. Do this even
                # when an assertion above failed, so a broken run leaves no
                # residue.
                if booked:
                    _s5, _d, _marker, _, _ = booked
                    try:
                        rr = _s5.get(cal_url.rstrip("/") + "/auth/login",
                                     timeout=T)
                        cj = "; ".join(c.name + "=" + c.value
                                        for c in _s5.cookies)
                        m = re.search(r"calrs_csrf=([^;]+)", cj)
                        tok = m.group(1) if m else ""
                        rr = _s5.post(cal_url.rstrip("/") + "/auth/login",
                                      data={"_csrf": tok,
                                            "email": cfg("CALRS_ADMIN_EMAIL"),
                                            "password": cfg("CALRS_ADMIN_PASSWORD")},
                                      timeout=T, allow_redirects=True)
                        dash_ok = "/dashboard" in rr.url
                        if not dash_ok:
                            result.warn("X5: dashboard login failed — cleanup "
                                        "may be incomplete")
                        rr = _s5.get(cal_url.rstrip("/") + "/dashboard/bookings",
                                     timeout=T)
                        rows = rr.text.split('class="booking-row"')
                        bid = None
                        for chunk in rows[1:]:
                            if _marker in chunk:
                                mm = re.search(r"/dashboard/bookings/"
                                               r"([0-9a-f-]{36})/cancel", chunk)
                                if mm:
                                    bid = mm.group(1)
                                break
                        if bid:
                            cj = "; ".join(c.name + "=" + c.value
                                            for c in _s5.cookies)
                            m = re.search(r"calrs_csrf=([^;]+)", cj)
                            tok = m.group(1) if m else ""
                            rr = _s5.post(cal_url.rstrip("/") +
                                          f"/dashboard/bookings/{bid}/cancel",
                                          data={"_csrf": tok,
                                                "reason": "x5 self-clean"},
                                          timeout=T, allow_redirects=True)
                            # ── X5.4: VEVENT must be DELETEd from Radicale
                            rr = _report_cal()
                            if rr.status_code == 207 and marker not in rr.text:
                                result.ok("X5: cancelling the booking DELETEd "
                                          "its VEVENT from Radicale (write-back "
                                          "delete propagates)")
                            else:
                                result.fail("X5: Radicale VEVENT survived the "
                                            f"cancel (REPORT {rr.status_code})")
                                x5_ok = False
                            # ── X5.5: mirror must drop it after re-sync
                            if got_events is not None:
                                ev0, _mark = got_events
                                # re-sync (fresh session kept in scope: reuse
                                # the s6 login side-effects below is messy —
                                # do a bare re-sync via the same s6 session)
                                try:
                                    s6.post(wm_url + "/api/user/v1/external-"
                                            "calendars/" + key6 + "/sync",
                                            headers=h6, timeout=T)
                                    for _ in range(10):
                                        time.sleep(6)
                                        st = s6.get(wm_url + "/api/user/v1/"
                                                    "external-calendars/" + key6
                                                    + "/sync", headers=h6, timeout=T)
                                        stj = st.json().get("data") or {}
                                        if stj.get("sync_status") == "completed":
                                            break
                                    lo = (booked[1] - datetime.timedelta(days=2)).isoformat()
                                    hi = (booked[1] + datetime.timedelta(days=3)).isoformat()
                                    ev = s6.get(wm_url + "/api/user/v1/calendars/"
                                                + key6 + "/events?start_date_time="
                                                + lo + "&end_date_time=" + hi,
                                                headers=h6, timeout=T)
                                    if marker not in ev.text:
                                        result.ok("X5: sogo6 mirror dropped the "
                                                  "cancelled booking after re-sync")
                                    else:
                                        result.fail("X5: cancelled booking still in "
                                                    "the sogo6 mirror")
                                        x5_ok = False
                                except Exception as ex:
                                    result.warn(f"X5: mirror-drop re-sync skipped "
                                                f"({ex})")
                        else:
                            result.warn("X5: cancelled booking not found on the "
                                        "dashboard — Radicale/mirror cleanup "
                                        "unverified")
                    except Exception as ex:
                        result.warn(f"X5: cleanup failed ({ex}) — booking may "
                                   "remain confirmed")

    # ══ Epic A — Archive (paperless-ngx) ══════════════════════════════
    result.header("Epic A — Archive (paperless-ngx)")
    pp_url = cfg("PAPERLESS_URL") or (cfg("PAPERLESS_DOMAIN") and
                                      schema + cfg("PAPERLESS_DOMAIN"))
    pp_url = pp_url or (f"{schema}paperless.{portal_host}" if portal_host else "")
    if not pp_url or is_localish(pp_url.rsplit("//", 1)[-1].split("/")[0]):
        result.skip("A: paperless host localish")
    else:
        ok, _ = reachable(anon, pp_url + "/")
        if not ok:
            result.skip(f"A: paperless not deployed ({pp_url})")
        else:
            r = anon.get(pp_url + "/", timeout=T, allow_redirects=False)
            if r.status_code in (301, 302, 303) and "/accounts/login" in \
                    r.headers.get("location", ""):
                result.ok("A1: anon / → 302 /accounts/login/")
            else:
                result.fail(f"A1: paperless / → {r.status_code}, want login redirect")
            r = anon.get(pp_url + "/accounts/login/", timeout=T)
            if r.status_code == 200 and "zitadel" in r.text.lower():
                result.ok("A2: login page carries the Zitadel OIDC affordance")
            else:
                result.fail(f"A2: login page → {r.status_code}, oidc marker missing")
            r = anon.get(pp_url + "/api/documents/", timeout=T)
            if r.status_code == 401:
                result.ok("A3: documents API refuses anonymous (401)")
            else:
                result.fail(f"A3: documents API → {r.status_code}, want 401")
            hardened(anon.get(pp_url + "/accounts/login/", timeout=T), result,
                     "A4 paperless", xfo_required=False)

    # ══ Epic G — Chat & Matrix (element + synapse) ════════════════════
    result.header("Epic G — Chat & Matrix (element + synapse)")
    chat_url = cfg("CHAT_URL") or (f"{schema}chat.{portal_host}" if portal_host else "")
    mx_url = cfg("MATRIX_URL") or (cfg("MATRIX_DOMAIN") and schema + cfg("MATRIX_DOMAIN"))
    mx_url = mx_url or (f"{schema}matrix.{portal_host}" if portal_host else "")
    if chat_url and not is_localish(chat_url.rsplit("//", 1)[-1].split("/")[0]):
        ok, _ = reachable(anon, chat_url + "/")
        if not ok:
            result.skip(f"G: chat not deployed ({chat_url})")
        else:
            r = anon.get(chat_url + "/", timeout=T)
            if r.status_code == 200 and "Element" in r.text:
                result.ok("G1: Element SPA served")
            else:
                result.fail(f"G1: chat / → {r.status_code}, want 200 Element")
            hardened(r, result, "G4 chat", xfo_required=False)
    else:
        result.skip("G1/G4: chat host localish")
    if mx_url and not is_localish(mx_url.rsplit("//", 1)[-1].split("/")[0]):
        r = anon.get(mx_url + "/.well-known/matrix/client", timeout=T)
        try:
            base = ((r.json() or {}).get("m.homeserver") or {}).get("base_url", "")
        except ValueError:
            base = ""
        if r.status_code == 200 and base.rstrip("/") == mx_url.rstrip("/"):
            result.ok(f"G2: .well-known/matrix/client advertises {base}")
        else:
            result.fail(f"G2: well-known → {r.status_code} {base!r} != {mx_url!r}")
        r = anon.get(mx_url + "/_matrix/client/versions", timeout=T)
        try:
            vs = (r.json() or {}).get("versions") or []
        except ValueError:
            vs = []
        if r.status_code == 200 and vs:
            result.ok(f"G3: synapse versions {vs[:3]}…")
        else:
            result.fail(f"G3: synapse versions → {r.status_code}, want JSON list")
    else:
        result.skip("G2/G3: matrix host localish")

    # ══ Epic I — IdP traits (zitadel) ═════════════════════════════════
    result.header("Epic I — IdP traits (zitadel)")
    r = anon.get(idp_base + "/.well-known/openid-configuration", timeout=T)
    try:
        disc = r.json()
    except ValueError:
        disc = {}
    endpoints_ok = all(disc.get(k) for k in
                       ("issuer", "authorization_endpoint", "token_endpoint",
                        "jwks_uri", "end_session_endpoint"))
    if r.status_code == 200 and disc.get("issuer", "").rstrip("/") \
            == idp_base.rstrip("/") and endpoints_ok:
        result.ok(f"I1: discovery issuer=={disc['issuer'][:44]}…, endpoints ok")
    else:
        result.fail(f"I1: discovery → {r.status_code} "
                    f"issuer={disc.get('issuer', '?')!r} endpoints={endpoints_ok}")
    r = anon.get(disc.get("jwks_uri") or idp_base + "/oauth/v2/keys", timeout=T)
    try:
        n_keys = len((r.json() or {}).get("keys") or [])
    except ValueError:
        n_keys = 0
    if r.status_code == 200 and n_keys >= 1:
        result.ok(f"I2: JWKS serves {n_keys} signing keys")
    else:
        result.fail(f"I2: JWKS → {r.status_code}, {n_keys} keys")
    r = anon.post(idp_base + "/oauth/v2/token",
                  data={"grant_type": "authorization_code", "code": "e2e-garbage"},
                  timeout=T)
    if 400 <= r.status_code < 500 and "error" in r.text:
        result.ok(f"I3: token endpoint rejects garbage grant ({r.status_code})")
    else:
        result.fail(f"I3: token endpoint → {r.status_code}, want 4xx JSON error")
    r = anon.get(idp_base + "/oidc/v1/end_session", timeout=T, allow_redirects=False)
    if r.status_code in (301, 302, 303) and "logout" in r.headers.get("location", ""):
        result.ok("I4: bare end_session → graceful logout redirect")
    else:
        result.fail(f"I4: end_session → {r.status_code} "
                    f"{r.headers.get('location', '')[:40]!r}")
    r = anon.get(idp_base + "/ui/v2/login", timeout=T)
    if r.status_code == 200:
        result.ok("I5: login UI served anonymously")
    else:
        result.fail(f"I5: login UI → {r.status_code}, want 200")

    # ══ Epic J — Signed-in SSO journeys (headless) ════════════════════
    result.header("Epic J — Signed-in SSO journeys (headless)")
    if not cfg("E2E_ZITADEL_LOGINCLIENT_PAT") and not is_localish(domain):
        result.warn("J: no login-client PAT — journeys limited to seeded fallback")
    lc = _lc_pat(anon)
    t_pw = cfg("E2E_TESTUSER_PASSWORD", "eval2026")  # documented demo cred
    t_uid = None
    try:
        t_uid = resolve_user_id(anon, idp_base,
                                cfg("E2E_ZITADEL_PAT") or lc, "testuser")
    except Exception:
        t_uid = None
    if not t_uid:
        result.skip("J: testuser not resolvable (demo stack only)")
    else:
        # J1 invoiceninja — the member's linked-account round trip
        # (env URL first, then the live <svc>.<portal_host> convention —
        # stale OPENSME_DOMAIN-style exports must not misroute us)
        inv = resolve_routable(anon, [cfg("INVOICE_URL"),
                                      f"{schema}invoices.{portal_host}"
                                      if portal_host else ""])
        s1 = requests.Session()
        s1.verify = verify
        authz = capture_authorize(s1, inv + "/auth/oidc") if inv else ""
        if authz:
            r = oidc_login(s1, idp_base, authz, t_uid, t_pw, lc)
            if r.status_code == 200 and "linked to this OIDC identity" not in r.text:
                result.ok(f"J1: invoiceninja OIDC round trip (member, "
                          f"{str(r.url).split('//')[1][:38]}…)")
            else:
                result.fail(f"J1: invoiceninja OIDC → {r.status_code} "
                            + ("identity not linked — invite the member first"
                               if "linked to this OIDC identity" in r.text
                               else str(r.url)[:60]))
        else:
            result.skip("J1: invoiceninja not routed")

        # J2 paperless — allauth confirm form → authorize → signed API
        pp = resolve_routable(anon, [cfg("PAPERLESS_URL"),
                                     f"{schema}paperless.{portal_host}"
                                     if portal_host else ""])
        s2 = requests.Session()
        s2.verify = verify
        authz = ""
        try:
            r0 = s2.get(pp + "/accounts/oidc/zitadel/login/", timeout=T)
            tok = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"',
                            r0.text)
            if tok and r0.status_code == 200:
                authz = capture_authorize(
                    s2, pp + "/accounts/oidc/zitadel/login/",
                    post=({"csrfmiddlewaretoken": tok.group(1)},
                          pp + "/accounts/oidc/zitadel/login/"))
        except requests.RequestException:
            authz = ""
        if authz:
            r = oidc_login(s2, idp_base, authz, t_uid, t_pw, lc)
            api_ok = False
            try:
                api_ok = s2.get(pp + "/api/documents/", timeout=T).status_code == 200
            except requests.RequestException:
                pass
            if r.status_code == 200 and api_ok:
                result.ok("J2: paperless OIDC → dashboard; documents API "
                          "readable for the OIDC user (default-group perms)")
            else:
                result.fail(f"J2: paperless → {r.status_code}, docs API "
                            f"{'200' if api_ok else 'DENIED (perms missing?)'}")
        else:
            result.skip("J2: paperless not routed")

        # J3 operaton — the full webapp chain, signed
        bpm = resolve_routable(anon, [cfg("BPM_URL"),
                                      f"{schema}bpm.{portal_host}"
                                      if portal_host else ""])
        s3 = requests.Session()
        s3.verify = verify
        authz = capture_authorize(s3, bpm + "/") if bpm else ""
        if authz:
            r = oidc_login(s3, idp_base, authz, t_uid, t_pw, lc)
            r = s3.get(bpm + "/operaton/app/", timeout=T)
            if r.status_code == 200:
                result.ok("J3: operaton SSO → signed webapp (200)")
            else:
                result.fail(f"J3: operaton signed webapp → {r.status_code}")
        else:
            result.skip("J3: BPM not routed")

        # J4 synapse — SSO redirect → login → loginToken handoff page
        mx = resolve_routable(anon, [cfg("MATRIX_URL"),
                                     f"{schema}matrix.{portal_host}"
                                     if portal_host else ""])
        chat = resolve_routable(anon, [cfg("CHAT_URL"),
                                       f"{schema}chat.{portal_host}"
                                       if portal_host else ""])
        s4 = requests.Session()
        s4.verify = verify
        authz = capture_authorize(
            s4, mx + "/_matrix/client/r0/login/sso/redirect"
                 f"?redirectUrl={urllib.parse.quote(chat + '/')}"
        ) if mx and chat else ""
        if authz:
            r = oidc_login(s4, idp_base, authz, t_uid, t_pw, lc)
            if r.status_code == 200 and "Continue to your account" in r.text:
                result.ok("J4: synapse SSO → loginToken handoff page")
            else:
                result.fail(f"J4: synapse SSO callback → {r.status_code}, "
                            "handoff page missing")
        else:
            result.skip("J4: matrix not routed")

    # ══ Epic T — Mail (stalwart) ══════════════════════════════════════
    result.header("Epic T — Mail (stalwart)")
    mail_url = resolve_routable(anon, [cfg("MAIL_URL"),
                                       f"{schema}mail.{portal_host}"
                                       if portal_host else ""])
    if not mail_url:
        result.skip("T: mail not routable")
    else:
        ok, _ = reachable(anon, mail_url + "/")
        if not ok:
            result.skip(f"T: stalwart not deployed ({mail_url})")
        else:
            r = anon.get(mail_url + "/", timeout=T)
            if r.status_code == 200:
                result.ok("T1: stalwart web UI served anonymously")
            else:
                result.fail(f"T1: mail / → {r.status_code}, want 200")
            hardened(anon.get(mail_url + "/", timeout=T), result, "T2 mail",
                     xfo_required=False)

    # I6 — authorize endpoint hardening: unknown client → 4xx (no 5xx)
    r = anon.get(idp_base + "/oauth/v2/authorize"
                 "?response_type=code&client_id=e2e-unknown-client"
                 "&redirect_uri=https://e2e.invalid/cb&scope=openid",
                 timeout=T, allow_redirects=False)
    if 400 <= r.status_code < 500:
        result.ok(f"I6: authorize refuses unknown client ({r.status_code})")
    else:
        result.fail(f"I6: unknown client → {r.status_code}, want 4xx")

    # ══ Epic Y — AI gating (portal) ═══════════════════════════════════
    result.header("Epic Y — AI gating (portal)")
    if cfg("AI_API_KEY"):
        result.warn("Y1: AI configured — disabled-route contract not testable")
    else:
        r = anon.post(portal_base + "/api/ai/chat", json={"question": "hi"}, timeout=T)
        if r.status_code == 404:
            result.ok("Y1: /api/ai/chat route absent while AI unconfigured (404)")
        else:
            result.warn(f"Y1: /api/ai/chat → {r.status_code} (route live?)")

    # ══ Epic E — Edge hardening ═══════════════════════════════════════
    result.header("Epic E — Edge hardening (security-headers shadow)")
    hardened(anon.get(portal_base + "/", timeout=T), result, "E1 portal")
    if vc_url:
        hardened(anon.get(vc_url + "/", timeout=T), result, "E2 vc")
    if cal_url:
        hardened(anon.get(cal_url + "/", timeout=T), result, "E3 calendar")
    for label, host in (("E4 chat", f"{schema}chat.{portal_host}"),
                        ("E4 surveys", f"{schema}surveys.{portal_host}")):
        ok, r = reachable(anon, host + "/")
        if ok:
            hardened(r, result, label)
        else:
            result.skip(f"{label}: not deployed ({host})")

    # ══ Epic S — Catalog surfaces ═════════════════════════════════════
    result.header("Epic S — Catalog surfaces (contracts/tiles.yml)")
    catalog = {}
    if CATALOG.exists():
        try:
            catalog = yaml.safe_load(CATALOG.read_text()) or {}
        except yaml.YAMLError as e:
            result.fail(f"S: tiles.yml unparseable: {e}")
    if not catalog:
        result.skip("S: no catalog loaded")
    else:
        for entry in catalog.get("catalog", []):
            tid = entry.get("id", "?")
            url_env, domain_env = entry.get("url_env"), entry.get("domain_env")
            prefix = entry.get("host_prefix")
            url = cfg(url_env) if url_env else ""
            candidates = []
            if domain_env and cfg(domain_env):
                candidates.append(f"{schema}{cfg(domain_env)}")
            if prefix:
                # live host conventions drifted: <svc>.<portal-host> and
                # <svc>.<domain> both exist — probe in order
                candidates += [f"{schema}{prefix}.{h}" for h in (portal_host, domain)]
            # explicit URL first, then convention candidates — first
            # host that actually routes wins
            for cand in ([url] if url else []) + candidates:
                ok, _ = reachable(anon, cand + "/", timeout=8)
                if ok:
                    url = cand
                    break
            else:
                url = ""
            if not url:
                result.skip(f"S/{tid}: not configured ({url_env} empty, no routed host)")
                continue
            ok, r = reachable(anon, url + "/")
            if not ok:
                result.skip(f"S/{tid}: configured but unreachable ({url}) — "
                            "overlay not deployed")
                continue
            if r.status_code >= 500:
                result.fail(f"S/{tid}: {url} answers {r.status_code} anonymously")
            else:
                result.ok(f"S/{tid}: {url} → {r.status_code}")
            hardened(r, result, f"S/{tid}", xfo_required=False)

    print()
    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
