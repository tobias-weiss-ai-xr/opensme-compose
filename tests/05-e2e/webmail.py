#!/usr/bin/env python3
"""
tests/05-e2e/webmail.py — SOGo webmail page contract (standalone journey).

Background (2026-10-12): the live webmail served a blank login page — the
HTML rendered, but every static asset under /SOGo.woa/WebServerResources/
returned 404. Root cause: a stale sogo container still ran with the old
Traefik labels (sogod :20000, which 404s WebServerResources) while
mail/sogo.yml correctly fronts sogod with apache2 on :80. Nothing in the
suite caught it: section_sogo_sso only checks the SSO redirect shape.

This journey pins the page contract:

  0. Precondition: SOGo is deployed. A stack that boots no mail profile
     (the demo stack — `scripts/demo.sh`) skips here instead of failing;
     a SOGo container that exists but does not answer /SOGo/ with the SSO
     redirect FAILS, which is the regression this journey is for (a
     container that silently lost its routing or its IdP wiring).
  2. The SSO redirect: an unauthenticated /SOGo/ must hand the visitor to
     the IdP's authorize endpoint with the configured client_id (the Epic C3
     contract — SOGo no longer owns a login form). Followed only across the
     http→https scheme hop and webmail-host hops; the IdP's login page is
     never fetched (its reachability is the e2e suite's business).
  3. SOGo's own database must contain its tables. A broken SQL layer (a
     password that is not percent-encoded into the postgresql:// URL, the
     wrong DB user) leaves the schema empty and the later steps impossible.
     Only checked when the local engine can reach opensme-postgres.
  4. An authenticated webmail session — the part a user actually notices.
     The journey drives the OIDC flow exactly like a browser: the IdP's
     Session API plays the login form (user check, password check,
     finalize), the callback is replayed against the webmail host for
     SOGo's server-side code exchange, and then the Angular app's first
     calls must serve: account bootstrap
     (/SOGo/so/<uid>/Mail/mailAccounts), mailbox view, INBOX view. The
     account list must carry an identity equal to the SSO uid <user>@<domain>
     — the identity contract: Zitadel's email claim = SOGo's mail_users
     c_uid = Stalwart's principal name = the IMAP/SMTP login name. FAILED
     when mail_users has rows and the login still does not work.

     Each action URL needs the session cookie set during the callback (a
     naive curl probe gets HTTP 500 "the called object returned no result"
     from SOGo's XSRF guard); X-XSRF-TOKEN is sent when the app's cookie
     jar carries one. SOGo binds the OIDC state to the session cookie it
     set during the redirect — the whole dance MUST run on one cookie jar.

  5. The authenticated app shell and its assets — the SSO-era equivalent of
     the 2026-10-12 blank-page regression: the redirect fires and the login
     works, but a Traefik label pointing at sogod instead of the apache
     front serves an empty shell whose every asset 404s.

  6. Outbound mail, as the same user, via XOAUTH2: SMTP submission on 587
     (the server advertises and REQUIRES STARTTLS — "requiretls") with an
     access token from a second authorize dance. Since the webmail SSO the
     OIDC directory is the default, so the demo password is refused on the
     mail ports — only a token the IdP validates works. Skips when the port
     is not reachable from the test host.

  7. Inbound mail, closing the round trip: the message submitted in step 6
     must appear in the INBOX over IMAP XOAUTH2 on 143. Only checked when
     step 6 delivered a message.

  8. Calendar and contacts over DAV — the rest of what "groupware" means.
     /SOGo/dav/** accepts only HTTP Basic (it rejects the SSO session
     cookie), with the SSO uid as the login name against the SQL source's
     SHA512-CRYPT hash — the one place the provisioned password still
     authenticates. The home set must list the user's Calendar/ and
     Contacts/ collections and each must expose at least one child. This
     step needs only SOGo and its database, so it still runs when the mail
     store makes steps 6/7 skip.

Usage:
  python3 tests/05-e2e/webmail.py                    # domain from .env
  python3 tests/05-e2e/webmail.py home.opensme.graphwiz.ai

The login user comes from bootstrap/demo-users.json (first user — the same
file scripts/provision-demo-users.sh feeds the IdP and SOGo from), overridable
with SOGO_E2E_USER / SOGO_E2E_PASSWORD. The SSO legs additionally need the
Zitadel machine PATs the bootstrap seeds (docker compose cp; override with
E2E_ZITADEL_PAT / E2E_ZITADEL_LOGINCLIENT_PAT) and the SOGo OIDC client
credentials from .env (SOGO_OIDC_CLIENT_ID / SOGO_OIDC_CLIENT_SECRET).
Steps 6-7 talk to the mail ports the compose stack publishes on the test
host (override with MAIL_E2E_HOST / MAIL_E2E_SUBMISSION_PORT /
MAIL_E2E_IMAP_PORT; the STARTTLS certificate is the server's self-signed
one, so it is not verified — same reason the overlay runs IMAP in clear
text on the compose network).

Exit codes: 0 = pass, 1 = failed (skips don't fail).
Usage:
  python3 tests/05-e2e/webmail.py                    # domain from .env
  python3 tests/05-e2e/webmail.py home.opensme.graphwiz.ai

Credentials for steps 5–7 come from bootstrap/demo-users.json (first user —
the same file scripts/provision-demo-users.sh feeds both stores from),
overridable with SOGO_E2E_USER / SOGO_E2E_PASSWORD. Steps 6–7 talk to the
mail ports the compose stack publishes on the test host (override with
MAIL_E2E_HOST / MAIL_E2E_SUBMISSION_PORT / MAIL_E2E_IMAP_PORT; the STARTTLS
certificate is the server's self-signed one, so it is not verified — same
reason the overlay runs IMAP in clear text on the compose network).

Exit codes: 0 = pass, 1 = failed (skips don't fail).
"""

import argparse
import base64
import imaplib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlsplit

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import (  # (path bootstrap before import)
    E2EError,
    T,
    api_login,
    fetch_seeded,
    install_dns_fallback,
    is_localish,
    load_env_file,
    resolve_user_id,
)

# Steps 6–7 talk to the mail ports the compose stack publishes on the test host
# (the webmail itself is probed through the edge, see webmail_base).
MAIL_HOST = os.environ.get("MAIL_E2E_HOST", "127.0.0.1")
SUBMISSION_PORT = int(os.environ.get("MAIL_E2E_SUBMISSION_PORT", "587"))
IMAP_PORT = int(os.environ.get("MAIL_E2E_IMAP_PORT", "143"))


def asset_urls(html: str, base: str) -> list[str]:
    """All local css/js/img asset URLs referenced by the page."""
    urls: list[str] = []
    for attr in ("href", "src"):
        for raw in re.findall(rf'{attr}="([^"]+)"', html):
            if not re.search(r"\.(css|js|ico|png|svg|woff2?)(\?|$)", raw):
                continue
            if raw.startswith(("http://", "https://", "data:", "mailto:")):
                continue
            urls.append(urljoin(base, raw))
    return sorted(set(urls))


def sogo_container_present() -> bool:
    """True when the local engine runs a `opensme-sogo` container.

    Only docker speaks here: the container name is fixed by mail/sogo.yml
    (`container_name: opensme-sogo`), and every deployment that fronts SOGo
    with this compose file has it. `docker ps` lists running AND
    restarting containers — a crash-looping SOGo counts as deployed (it must
    be caught), a container that was created but never started does not.
    When the engine is unreachable (or the host uses another runtime) we
    cannot tell — treat it as "not deployed" so the journey skips instead of
    inventing a failure.
    """
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return "opensme-sogo" in proc.stdout.split()


def stalwart_container_present() -> bool:
    """True when the local engine runs a `opensme-stalwart` container.

    Steps 6–7 reach the mail ports on the host, not through the edge. When the
    container runs, mail/stalwart.yml published 25/143/587 — so an unreachable
    port is a defect (listener unbound, ports dropped from the overlay), not a
    remote deployment without published ports. Same distinction the SOGo
    precondition makes: skip only when there is nothing that should answer.
    """
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return "opensme-stalwart" in proc.stdout.split()


def sogo_schema_tables() -> int | None:
    """How many tables SOGo created in its own database, or None if unknown.

    The page renders from templates; the login backend does not. When SOGo's
    SQL URLs are wrong (unencoded password, superuser credentials, missing
    SOGoProfileURL) the page still answers 200 while every query fails and the
    database stays empty — the state that produced
      "failed to acquire channel for URL: (null)"
    for weeks. A working stack always has sogo_folder_info, sogo_user_profile
    and sogo_sessions_folder after the first start, so "0 tables" is never a
    timing artefact.

    Returns None when the engine or the postgres container is out of reach
    (remote deployment) — that is not a verdict about SOGo.
    """
    try:
        proc = subprocess.run(
            ["docker", "exec", "opensme-postgres",
             "psql", "-U", "opensme", "-d", "sogo", "-tAc",
             ("select count(*) from information_schema.tables "
              "where table_schema = 'public'")],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def sogo_mail_user_rows() -> int | None:
    """How many webmail users SOGo can authenticate, or None if unknowable.

    sogo.mail_users is the table SOGoUserSources points at (see
    postgres-init/02-mail-users.sql). Zero rows is a legitimate state — a
    fresh stack that never ran scripts/provision-demo-users.sh --component
    stalwart — and then the login leg below cannot be judged. A row count
    above zero makes a failing login a real defect, not a missing fixture.

    Returns None when the engine or the postgres container is out of reach
    (remote deployment).
    """
    user = "opensme"
    try:
        env = subprocess.run(
            ["docker", "exec", "opensme-postgres", "printenv", "POSTGRES_USER"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if env.returncode == 0 and env.stdout.strip():
            user = env.stdout.strip()
        proc = subprocess.run(
            ["docker", "exec", "opensme-postgres",
             "psql", "-U", user, "-d", "sogo", "-tAc",
             "select count(*) from mail_users"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def demo_mail_credentials(root: Path) -> tuple[str, str] | None:
    """Login/password of the first demo user (env overrides for deployments).

    bootstrap/demo-users.json is the single source of truth: the same entry
    seeds Stalwart AND sogo.mail_users via scripts/provision-demo-users.sh
    --component stalwart, so a test that reads it cannot drift from the
    provisioning.
    """
    user = os.environ.get("SOGO_E2E_USER", "").strip()
    password = os.environ.get("SOGO_E2E_PASSWORD", "").strip()
    if user and password:
        return user, password
    try:
        users = json.loads((root / "bootstrap" / "demo-users.json").read_text())["users"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not users:
        return None
    first = users[0]
    return first.get("username", ""), first.get("password", "")


def oauth_sasl(token: str) -> str:
    """The XOAUTH2 SASL challenge for an access token (IMAP and SMTP share it)."""
    return "user=\x01auth=Bearer " + token + "\x01\x01"


def sso_redirect_target(result, session, webmail_base: str) -> tuple[str | None, str]:
    """Walk /SOGo/'s redirect hops and return (authorize_url, error).

    Walks redirect hops that stay on the webmail host (the :80 scheme
    upgrade fires before SOGo's router); the SSO redirect is the hop that
    leaves to the IdP's authorize endpoint with a client_id in tow. Fails
    the journey when the stack serves something else — the Epic C3 contract
    has SOGo delegate authentication to Zitadel, there is no local form.
    """
    url = webmail_base + "/SOGo/"
    try:
        r = session.get(url, allow_redirects=False, timeout=T)
    except requests.RequestException as e:
        return None, f"{type(e).__name__}: {e}"
    for _ in range(4):
        loc = r.headers.get("Location", "")
        if r.status_code in (301, 302, 303, 307, 308) and "/oauth" in loc \
                and "client_id=" in loc:
            return urljoin(url, loc), ""
        if r.status_code in (301, 302, 303, 307, 308) and loc \
                and urlsplit(urljoin(url, loc)).netloc == urlsplit(url).netloc:
            url = urljoin(url, loc)
            try:
                r = session.get(url, allow_redirects=False, timeout=T)
            except requests.RequestException as e:
                return None, f"{type(e).__name__}: {e}"
            continue
        return None, f"HTTP {r.status_code} {loc[:60] or '(no Location)'}"
    return None, f"redirect loop at {url[:70]}"


def sso_login(result, session, webmail_base: str, authorize_url: str, login: str,
              password: str, pat: str, lc_pat: str, client_id: str):
    """Drive SOGo's SSO exactly like a browser and return the web session.

    (1) the Session API logs the demo user in against the authorize request
    SOGo handed out (run.py's api_login: user check, password check, finalize),
    (2) the callback URL is replayed against the webmail host walking only
    webmail-host hops (the callback carries the http face → Traefik answers
    301 first). Everything runs on ONE session: SOGo binds the OIDC state to
    the session cookie it set during the redirect, so the callback MUST come
    back with that cookie — a fresh jar dies with "NULL string" → HTTP 501.
    Returns the same session (now carrying SOGo's login cookies) plus the
    XSRF header dict for the Angular app's calls.
    """
    idp_origin = urlsplit(authorize_url)._replace(path="", query="", fragment="").geturl()
    web = session
    user_id = resolve_user_id(web, idp_origin, pat, login)
    if not user_id:
        raise E2EError(f"no Zitadel user for {login} (machine PAT reached the IdP, "
                       "but the user search came back empty — is the zitadel "
                       "component of scripts/provision-demo-users.sh provisioned?)")
    qs, sess = api_login(web, idp_origin, lc_pat, client_id, authorize_url, user_id, password)
    # Rebuild the callback URL from the authorize request's redirect_uri.
    az = parse_qs(urlparse(authorize_url).query)
    callback = az.get("redirect_uri", [""])[0]
    if not callback:
        raise E2EError("authorize URL carries no redirect_uri")
    cb_url = callback + ("&" if "?" in callback else "?") + urlencode(qs, doseq=True)
    url = cb_url
    base_host = urlsplit(webmail_base).netloc
    for _ in range(10):
        r = web.get(url, allow_redirects=False, timeout=T)
        loc = r.headers.get("Location", "")
        if r.status_code in (301, 302, 303, 307, 308) and loc \
                and base_host in urljoin(url, loc):
            url = urljoin(url, loc)
            continue
        break
    if r.status_code not in (200, 302):
        raise E2EError(
            f"SOGo rejected the OIDC callback: HTTP {r.status_code} at {url[:80]} "
            "(redirect URI registered for the SOGo app? clock skew? code replayed?)"
        )
    xsrf = web.cookies.get("XSRF-TOKEN", "")
    return web, {"Accept": "application/json", **({"X-XSRF-TOKEN": xsrf} if xsrf else {})}


def mint_access_token(authorize_url: str, login: str, password: str,
                      pat: str, lc_pat: str, client_id: str,
                      client_secret: str, verify: bool = True) -> str:
    """A fresh access token via a second authorize dance + the token endpoint.

    The mail legs (steps 6/7) need their own token: the first dance's code was
    consumed by SOGo, and Stalwart validates each access token against the
    IdP's userinfo endpoint. Uses the confidential-client Basic auth at
    discovery's token_endpoint — the same exchange SOGo performs, kept in the
    journey so a broken client secret fails here and not in production.
    """
    idp_origin = urlsplit(authorize_url)._replace(path="", query="", fragment="").geturl()
    web = requests.Session()
    web.verify = verify
    user_id = resolve_user_id(web, idp_origin, pat, login)
    if not user_id:
        raise E2EError(f"no Zitadel user for {login} on the token dance")
    qs, _ = api_login(web, idp_origin, lc_pat, client_id, authorize_url, user_id, password)
    disc = web.get(f"{idp_origin}/.well-known/openid-configuration", timeout=T).json()
    tr = web.post(
        disc["token_endpoint"],
        auth=(client_id, client_secret),
        data={
            "grant_type": "authorization_code",
            "code": qs["code"][0],
            "redirect_uri": parse_qs(urlparse(authorize_url).query).get(
                "redirect_uri", [""])[0],
        },
        timeout=T,
    )
    if tr.status_code != 200 or "access_token" not in tr.json():
        raise E2EError(
            f"token endpoint: HTTP {tr.status_code} {tr.text[:80]} — client "
            "secret (SOGO_OIDC_CLIENT_SECRET) or redirect URI registration?"
        )
    return tr.json()["access_token"]


def submission_send(
    result, login: str, address: str, token: str, strict: bool = False
) -> str | None:
    """Submit a uniquely-subjected message to self over 587 + STARTTLS.

    Returns the subject when the server accepted the message, None when the
    leg was skipped or already reported as failed (the caller then does not
    judge delivery either).
    """
    subject = f"openSME e2e {int(time.time())}"
    # The listener requires STARTTLS but presents the server's self-signed
    # certificate (SAN localhost) — verifying it would test rcgen, not mail.
    tls = ssl.create_default_context()
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    try:
        server = smtplib.SMTP(MAIL_HOST, SUBMISSION_PORT, timeout=T)
    except OSError as e:
        unreachable = f"submission {MAIL_HOST}:{SUBMISSION_PORT} unreachable ({e})"
        if strict:
            result.fail(
                f"{unreachable} while opensme-stalwart runs — mail/stalwart.yml "
                "publishes 587, so the listener is unbound or the port was dropped"
            )
        else:
            result.skip(f"{unreachable} — sending not judged (mail profile not on this host)")
        return None
    try:
        server.ehlo()
        if not server.has_extn("starttls"):
            result.fail(
                f"submission {MAIL_HOST}:{SUBMISSION_PORT} advertises no STARTTLS "
                "— credentials would cross the wire in clear text"
            )
            return None
        server.starttls(context=tls)
        server.ehlo()
        # XOAUTH2 with the SSO access token: since the webmail SSO (Epic C3)
        # the OIDC directory is the default, and PLAIN credentials verify app
        # passwords only — the demo password is deliberately refused here.
        typ, resp = server.docmd("AUTH", "XOAUTH2 " + base64.b64encode(oauth_sasl(token).encode()).decode())
        if typ != 235:
            raise smtplib.SMTPAuthenticationError(typ, resp)
    except (smtplib.SMTPException, ssl.SSLError, OSError) as e:
        result.fail(
            f"submission XOAUTH2 as {login} failed: {type(e).__name__}: {e} "
            f"(SMTP {getattr(e, 'smtp_code', '-')}) — an SSO access token must "
            "authenticate, the OIDC directory validates it against the IdP"
        )
        return None
    message = EmailMessage()
    message["From"] = address
    message["To"] = address
    message["Subject"] = subject
    message.set_content(
        "Round trip sent by tests/05-e2e/webmail.py — step 7 expects this in INBOX."
    )
    try:
        server.send_message(message)
    except (smtplib.SMTPException, OSError) as e:
        result.fail(f"submission refused the message from {address}: {type(e).__name__}: {e}")
        return None
    finally:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            pass
    result.ok(f"submission accepts mail from {address} ({MAIL_HOST}:{SUBMISSION_PORT} + STARTTLS)")
    return subject


def inbox_round_trip(
    result, login: str, token: str, subject: str, strict: bool = False
) -> None:
    """The message from submission_send must land in the user's INBOX.

    Sending and delivering are different code paths in the server (submission
    vs. the delivery queue/credential for the recipient), so a green send can
    still leave the mailbox empty. IMAP runs in clear text here on purpose:
    Stalwart only allows plain-text LOGIN because SOGo's IMAP client cannot
    verify the self-signed certificate.
    """
    try:
        box = imaplib.IMAP4(MAIL_HOST, IMAP_PORT)
    except OSError as e:
        unreachable = f"IMAP {MAIL_HOST}:{IMAP_PORT} unreachable ({e})"
        if strict:
            result.fail(
                f"{unreachable} while opensme-stalwart runs — mail/stalwart.yml "
                "publishes 143, so the listener is unbound or the port was dropped"
            )
        else:
            result.skip(f"{unreachable} — delivery not judged")
        return
    try:
        sasl = oauth_sasl(token).encode()
        box.authenticate("XOAUTH2", lambda _: sasl)
        box.select("INBOX")
        for _ in range(15):
            typ, data = box.search(None, "HEADER", "Subject", f'"{subject}"')
            ids = data[0].split() if typ == "OK" and data and data[0] else []
            if ids:
                # Readable before deleting, so the check cannot pass on a
                # half-stored message, and the probe does not accumulate.
                box.fetch(ids[-1], "(BODY.PEEK[HEADER])")
                box.store(ids[-1], "+FLAGS", "\\Deleted")
                box.expunge()
                result.ok(
                    f"the submitted message arrives in the INBOX of {login} "
                    f"(IMAP {MAIL_HOST}:{IMAP_PORT}, then removed again)"
                )
                return
            time.sleep(2)
        result.fail(
            f"message accepted on submission but never appeared in {login}'s "
            f"INBOX after 30 s (subject {subject}) — sending works, delivery does not"
        )
    except imaplib.IMAP4.error as e:
        result.fail(f"IMAP XOAUTH2/select as {login} failed: {type(e).__name__}: {e}")
    finally:
        try:
            box.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def dav_groupware(result, base: str, verify: bool, login: str, password: str) -> None:
    """Calendar and contacts over DAV — the other half of the groupware.

    Deliberately a different auth surface: /SOGo/dav/** rejects the JSON
    session cookie and only accepts HTTP Basic, so a stack whose webmail login
    works can still have dead DAV — and calendar/contacts clients have no
    fallback for that. Depth 1 on the home set must list the user's own
    Calendar/ and Contacts/ collections (SOGo creates the defaults on first
    access), and each must return at least one child: that is the database
    layer serving real collections, not a stub 207.
    """
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:propfind xmlns:D="DAV:"><D:prop><D:displayname/>'
        '<D:resourcetype/></D:prop></D:propfind>'
    )
    depth1 = {"Depth": "1", "Content-Type": "application/xml"}

    def propfind(path: str):
        return requests.request(
            "PROPFIND",
            base + path,
            data=body,
            auth=(login, password),
            verify=verify,
            timeout=T,
            headers=depth1,
        )

    home = f"/SOGo/dav/{login}/"
    try:
        r = propfind(home)
    except requests.RequestException as e:
        result.fail(f"PROPFIND {home} failed: {type(e).__name__}: {e}")
        return
    if r.status_code != 207:
        result.fail(
            f"PROPFIND {home} → HTTP {r.status_code} (expected 207 Multi-Status; "
            "DAV takes HTTP Basic, not the webmail session cookie)"
        )
        return
    hrefs = re.findall(r"<(?:\w+:)?href>([^<]+)</(?:\w+:)?href>", r.text)
    for name in ("Calendar", "Contacts"):
        if not any(h.rstrip("/").endswith(name) for h in hrefs):
            result.fail(f"DAV home set {home} lists no {name}/ collection (hrefs: {hrefs[:5]})")
            return
    result.ok(f"DAV home set lists the user's collections ({len(hrefs) - 1} entries)")

    for name in ("Calendar", "Contacts"):
        path = f"{home}{name}/"
        try:
            c = propfind(path)
        except requests.RequestException as e:
            result.fail(f"PROPFIND {path} failed: {type(e).__name__}: {e}")
            continue
        if c.status_code != 207:
            result.fail(f"PROPFIND {path} → HTTP {c.status_code} (expected 207)")
            continue
        kids = [
            h
            for h in re.findall(r"<(?:\w+:)?href>([^<]+)</(?:\w+:)?href>", c.text)
            if h.rstrip("/") != path.rstrip("/")
        ]
        if not kids:
            result.fail(f"{path} exposes no collection — the database layer serves nothing")
            continue
        result.ok(f"{name} over DAV: {len(kids)} collection(s) reachable")


def main() -> bool:
    ap = argparse.ArgumentParser(description="openSME webmail page contract")
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
    # An explicit domain arg targets a specific deployment — it must win
    # over shell env and repo-local .env (both may point at the local demo).
    sogo_host = (
        f"webmail.{domain}" if args.domain
        else (os.environ.get("SOGO_DOMAIN") or cfg("SOGO_DOMAIN", f"webmail.{domain}"))
    )
    webmail_base = f"{schema}{sogo_host}"

    session = requests.Session()
    session.verify = not localish

    result = Result("webmail-page")
    result.header(f"SOGo groupware journey (webmail + mail server + DAV) — {webmail_base}")

    # 0. Precondition — the SSO contract (Epic C3): an unauthenticated /SOGo/
    #    must REDIRECT to the IdP's authorize endpoint. There is no SOGo login
    #    page anymore, so the probe must NOT follow redirects past the IdP hop
    #    (the IdP's login page itself is out of scope here — the Session API
    #    plays the login in step 2). "Deployed" means: the redirect fires with
    #    an OAuth shape. Anything else with the container present is the
    #    2026-10-12 routing-regression class; without a container, skip.
    authorize_url, probe_error = sso_redirect_target(result, session, webmail_base)
    if authorize_url is None:
        if sogo_container_present():
            result.fail(
                f"opensme-sogo container exists but GET /SOGo/ → {probe_error} "
                "— deployed SOGo must redirect unauthenticated visitors to the "
                "IdP's authorize endpoint (routing or SSO wiring regression)"
            )
        else:
            result.skip(
                f"SOGo not deployed ({webmail_base}/SOGo/ → {probe_error}, no "
                "opensme-sogo container) — boot the mail profile to run this"
            )
        return result.summary()

    # 1. SOGo owns its database — the page contract cannot judge this before
    #    authentication, and the page renders from templates, so SQL breakage
    #    (the percent-encoded-password defect) only shows in later steps.
    #    Checked here because everything after depends on a working schema.
    tables = sogo_schema_tables()
    if tables == 0:
        result.fail(
            "SOGo created no tables in its database — the SQL layer is dead "
            "(check the URL-encoded password and sogo_user in sogo.conf)"
        )
    elif tables is not None:
        result.ok(f"SOGo owns its database schema ({tables} tables)")


    # 2. a real webmail session — over the webmail SSO (Epic C3). SOGo no
    #    longer owns a login form: unauthenticated visitors are redirected to
    #    the IdP's authorize endpoint and the session comes back through the
    #    OIDC callback. The journey drives exactly that flow: the Session API
    #    (run.py's api_login) plays the login form, the callback is replayed
    #    against the webmail host for the server-side code exchange, and the
    #    Angular app's first calls must then name the user's own address —
    #    now the SSO uid (<user>@domain), which is Zitadel's email claim,
    #    SOGo's mail_users.c_uid AND Stalwart's principal name at once.
    #    Needs the seeded Zitadel PATs (docker compose cp, run.py's
    #    fetch_seeded) and the SOGo OIDC client credentials from .env.
    creds = demo_mail_credentials(Path(__file__).resolve().parent.parent.parent)
    rows = sogo_mail_user_rows()
    pat = os.environ.get("E2E_ZITADEL_PAT", "").strip() or fetch_seeded("/machinekey/pat")
    lc_pat = os.environ.get("E2E_ZITADEL_LOGINCLIENT_PAT", "").strip() \
        or fetch_seeded("/machinekey/login-client.pat")
    client_id = cfg("SOGO_OIDC_CLIENT_ID")
    client_secret = cfg("SOGO_OIDC_CLIENT_SECRET")
    if creds is None or not all(creds):
        result.skip("no demo user known (bootstrap/demo-users.json missing?)")
    elif not all((pat, lc_pat, client_id, client_secret)):
        missing = [n for n, v in (
            ("a Zitadel machine PAT", pat),
            ("a login-client PAT", lc_pat),
            ("SOGO_OIDC_CLIENT_ID", client_id),
            ("SOGO_OIDC_CLIENT_SECRET", client_secret),
        ) if not v]
        result.skip(
            f"SSO login not judgeable — no {', '.join(missing)}: mint the SOGo "
            "app (scripts/zitadel-client.sh --app sogo) and provision the demo "
            "users (scripts/provision-demo-users.sh)"
        )
    else:
        login, password = creds
        # The identity contract (Epic C3): SOGo logs the user in as the SSO
        # uid, which is the Zitadel email claim AND the IMAP login name.
        sso_uid = f"{login}@{domain}"
        # (a) the authorize URL from step 0 is the request the login dance
        #     attaches the session to; its client_id is already proven.
        az_qs = parse_qs(urlparse(authorize_url).query)
        if az_qs.get("client_id", [""])[0] != client_id:
            result.fail(
                f"SSO redirect targets client {az_qs.get('client_id', ['?'])[0]} "
                f"but .env configures {client_id} — SOGo and the registered "
                "OIDC app have drifted apart"
            )
        else:
            result.ok("/SOGo/ delegates authentication to the IdP (authorize)")
            web = None
            try:
                web, headers = sso_login(
                    result, session, webmail_base, authorize_url,
                    login, password, pat, lc_pat, client_id,
                )
            except E2EError as e:
                if rows:
                    result.fail(
                        f"SSO login as {login} failed: {e} — while "
                        f"sogo.mail_users holds {rows} row(s), so the login "
                        "must work (IdP user, client secret or callback?)"
                    )
                else:
                    result.skip(
                        f"SSO login as {login} failed ({e}) and no mail user "
                        "is provisioned — nothing to authenticate against"
                    )
            else:
                result.ok(f"SSO login as {login} succeeds (OIDC callback → session)")

                # 3. the authenticated app shell + its assets — the SSO-era
                #    equivalent of the 2026-10-12 blank-page regression: the
                #    redirect fires and the login works, but a Traefik label
                #    pointing at sogod instead of the apache front serves an
                #    empty shell whose every asset 404s. The shell references
                #    css/js/img under WebServerResources — all must load.
                try:
                    shell = web.get(webmail_base + "/SOGo/", headers=headers, timeout=T)
                except requests.RequestException as e:
                    result.fail(f"GET /SOGo/ (session) → {type(e).__name__}: {e}")
                    shell = None
                if shell is not None and (shell.status_code != 200 or "SOGo" not in shell.text):
                    result.fail(
                        f"authenticated /SOGo/ → {shell.status_code} without "
                        "app markup — the SSO session does not reach the app"
                    )
                elif shell is not None:
                    shell_assets = asset_urls(shell.text, webmail_base)
                    broken = []
                    for asset in shell_assets:
                        try:
                            ar = web.get(asset, headers=headers, timeout=T)
                        except requests.RequestException as e:
                            broken.append(f"{asset} ({e.__class__.__name__})")
                            continue
                        if ar.status_code != 200:
                            broken.append(f"{asset} → {ar.status_code}")
                    if not shell_assets:
                        result.warn(
                            "authenticated shell references no assets — "
                            "manual look recommended (blank-page class)"
                        )
                    elif broken:
                        result.fail(f"{len(broken)}/{len(shell_assets)} shell assets broken:")
                        for b in broken:
                            result.info(f"  {b}")
                    else:
                        result.ok(f"authenticated shell renders, all {len(shell_assets)} assets serve 200")

                identity_email = ""
                actions = [
                    (f"/SOGo/so/{sso_uid}/Mail/mailAccounts",
                     "account bootstrap (identities)"),
                    (f"/SOGo/so/{sso_uid}/Mail/0/view", "mailbox view"),
                    (f"/SOGo/so/{sso_uid}/Mail/0/folderINBOX/view", "INBOX view"),
                ]
                for path, label in actions:
                    try:
                        ar = web.get(webmail_base + path, headers=headers, timeout=T)
                    except requests.RequestException as e:
                        result.fail(f"GET {path} → {type(e).__name__}: {e}")
                        continue
                    if ar.status_code != 200:
                        hint = ""
                        if ar.status_code == 500 and "no result" in ar.text:
                            hint = (
                                " — SOGo's XSRF guard rejected the action; the "
                                "X-XSRF-TOKEN header must carry the XSRF-TOKEN "
                                "cookie (mailAccounts, Mail/0/view, Calendar … all "
                                "answer 500 this way)"
                            )
                        result.fail(f"GET {path} → {ar.status_code}{hint}")
                        continue
                    if label.startswith("account"):
                        try:
                            accounts = ar.json()
                        except ValueError:
                            accounts = None
                        if not isinstance(accounts, list) or not accounts:
                            result.fail(
                                f"{label} → {ar.status_code} but no mail account "
                                "(empty list: SOGo logged in, IMAP/Stalwart did not "
                                "— is the Stalwart principal provisioned? The OIDC "
                                "directory auto-provisions it on first login)"
                            )
                            continue
                        emails = [
                            i.get("email", "")
                            for a in accounts if isinstance(a, dict)
                            for i in (a.get("identities") or []) if isinstance(i, dict)
                        ]
                        if not any(sso_uid in e for e in emails):
                            result.fail(
                                f"{label} → account list carries no identity for "
                                f"{sso_uid} (identities: {emails[:3]}) — the SSO "
                                "uid must equal the mail address (identity contract)"
                            )
                            continue
                        result.ok(
                            f"{label}: {len(accounts)} mail account(s), "
                            f"identity {emails[0]}"
                        )
                        identity_email = next(e for e in emails if sso_uid in e)
                    else:
                        result.ok(f"{label} serves 200")

            # 4./5. the mail server behind the webmail: send and receive, with
            #       the same user and a FRESH access token — the code from the
            #       login dance was consumed by SOGo, and Stalwart validates
            #       each token against the IdP (the OIDC directory). The plain
            #       demo password is deliberately refused on these ports.
            if not identity_email:
                result.skip(
                    "account bootstrap returned no address for the user — "
                    "submission/delivery not judged"
                )
            else:
                try:
                    token = mint_access_token(
                        authorize_url, login, password, pat, lc_pat,
                        client_id, client_secret, not localish,
                    )
                except (E2EError, requests.RequestException, ValueError) as e:
                    result.fail(f"no access token for the mail legs: {e}")
                else:
                    result.ok("second authorize dance mints an access token")
                    strict = stalwart_container_present()
                    subject = submission_send(result, sso_uid, identity_email, token, strict)
                    if subject:
                        inbox_round_trip(result, sso_uid, token, subject, strict)

            # 6. the rest of the groupware: calendar + contacts over DAV. Runs
            #    whenever the SSO login worked — it needs SOGo and its database,
            #    not the IMAP store steps 6/7 depend on. The SQL source keys
            #    rows by the SSO uid (identity contract), so Basic auth uses it
            #    with the demo password (Dovecot-scheme hash, provisioned by
            #    scripts/provision-demo-users.sh).
            if web is not None:
                dav_groupware(result, webmail_base, not localish, sso_uid, password)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
