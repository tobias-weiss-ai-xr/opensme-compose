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
E2E_ZITADEL_PAT, E2E_ZITADEL_LOGINCLIENT_PAT, PORTAL_OIDC_CLIENT_ID.

Usage:
  python3 tests/05-e2e/stories.py                  # domain from .env
  python3 tests/05-e2e/stories.py opensme.local

Exit codes: 0 = pass, 1 = failures (skips never fail).
"""

import argparse
import base64
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
    install_dns_fallback,
    is_localish,
    load_env_file,
    portal_login,
)

CATALOG = ROOT / "contracts" / "tiles.yml"


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
                    result.ok(f"P8: Meet tile gated in, href == VC_URL")
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
        bad = [u for u in hostile if (res := session.get(vc_url + u, timeout=T)).status_code != 404]
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
            # documented quirk: negative option is rejected by the usize
            # extractor as a raw 422 before the handler's JSON 400
            r = member.post(portal_base + f"/api/polls/{pid}/vote", json={"option": -1},
                            timeout=T)
            if r.status_code == 422:
                result.warn("L5: polls option=-1 → raw 422 (usize extractor; "
                            "cosmetic quirk, pinned)")
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
        leak_markers = [m for m in ("password", "secret", "token", "set-cookie",
                                    "session=") if m in r.text.lower()]
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
