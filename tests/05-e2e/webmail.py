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
     a SOGo container that exists but does not answer /SOGo/ FAILS, which
     is the regression this journey is for (a container that silently
     lost its routing).
  1. GET /            → 301 (permanent) to /SOGo/ (root-redirect middleware)
  2. GET /SOGo/       → 200 with SOGo login markup
  3. Every asset the page references (css/js/img under WebServerResources)
     must resolve with 200 — a blank page fails here, even when the HTML
     and the SSO redirect look healthy.
  4. SOGo's own database must contain its tables. The page renders from
     templates, so 1–3 pass while every SQL query fails: a password that is
     not percent-encoded into the postgresql:// URL (or the wrong DB user)
     leaves the schema empty and login impossible. Only checked when the
     local engine can reach opensme-postgres.
  5. An authenticated webmail session — the part a user actually notices.
     SOGo authenticates against Postgres (sogo.mail_users), Stalwart owns
     IMAP; a half-provisioned user logs in and then sees an empty mailbox.
     So: POST /SOGo/connect (JSON), then the account bootstrap the Angular
     app calls first, GET /SOGo/so/<user>/Mail/mailAccounts, then the mailbox
     view. The account list must be non-empty JSON carrying the user's own
     address — that is the difference between "webmail works" and "webmail
     renders". Skipped when no mail user is provisioned (fresh stack); FAILED
     when mail_users has rows and the login still does not work.

     Each action URL needs TWO things a browser sends on its own, and a naive
     curl probe (no header) gets HTTP 500 with the 36-byte body "the called
     object returned no result" — SOGo's XSRF guard in UIxComponent
     -performActionNamed: refuses to dispatch without them:
       * the session cookie from POST /SOGo/connect, and
       * X-XSRF-TOKEN: <value of the XSRF-TOKEN cookie set by that login>.
     The login itself is exempt, which is what makes the artefact so
     confusing: the page, the assets and the login all answer 200 while every
     single action — Mail, Calendar, Contacts — answers 500.

  6. Outbound mail, as the same user: SMTP submission on 587 (the server
     advertises and REQUIRES STARTTLS — "requiretls", so a client that
     speaks plaintext is refused) with the credentials that just logged in.
     A stack where the webmail renders and lists an account can still be
     unable to send: the Stalwart role `user` carries `email-send` and the
     `smtp-*` permissions, and a principal without them is refused at 235
     vs 535. Skips when the port is not reachable from the test host.

  7. Inbound mail, closing the round trip: the message submitted in step 6
     must appear in the INBOX over IMAP on 143 (Stalwart refuses cleartext
     LOGIN unless [imap.auth] allow-plain-text is set, which the overlay
     does set because SOGo's IMAP client cannot verify the self-signed
     certificate). Only checked when step 6 delivered a message.

  8. Calendar and contacts over DAV — the rest of what "groupware" means.
     /SOGo/dav/** accepts only HTTP Basic (it rejects the JSON session
     cookie), so a stack whose webmail works can still have dead DAV, with
     no fallback in any calendar/contacts client. The home set must list
     the user's Calendar/ and Contacts/ collections and each must expose at
     least one child (SOGo creates the defaults on first access). This step
     needs only SOGo and its database, so it still runs when the mail store
     makes steps 6/7 skip.

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
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import (  # noqa: E402  (path bootstrap before import)
    T,
    install_dns_fallback,
    is_localish,
    load_env_file,
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
            capture_output=True, text=True, timeout=15,
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
            capture_output=True, text=True, timeout=15,
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
             "select count(*) from information_schema.tables "
             "where table_schema = 'public'"],
            capture_output=True, text=True, timeout=30,
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
            capture_output=True, text=True, timeout=15,
        )
        if env.returncode == 0 and env.stdout.strip():
            user = env.stdout.strip()
        proc = subprocess.run(
            ["docker", "exec", "opensme-postgres",
             "psql", "-U", user, "-d", "sogo", "-tAc",
             "select count(*) from mail_users"],
            capture_output=True, text=True, timeout=30,
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


def submission_send(
    result, login: str, address: str, password: str, strict: bool = False
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
        server.login(login, password)
    except (smtplib.SMTPException, ssl.SSLError, OSError) as e:
        result.fail(
            f"submission login as {login} failed: {type(e).__name__}: {e} "
            f"(SMTP {getattr(e, 'smtp_code', '-')}) — the role `user` carries "
            "email-send, a principal without it is refused here"
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
    result, login: str, password: str, subject: str, strict: bool = False
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
        box.login(login, password)
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
        result.fail(f"IMAP login/select as {login} failed: {type(e).__name__}: {e}")
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

    # 0. Precondition — is SOGo deployed at all?
    # A deployed SOGo answers /SOGo/ with 200 (check 2 below). When it does not
    # answer, only the presence of the container distinguishes "no mail profile
    # booted" (skip — the demo stack ships neither Stalwart nor SOGo) from
    # "deployed but no longer routed" (fail — the 2026-10-12 regression).
    try:
        probe = session.get(webmail_base + "/SOGo/", timeout=T)
    except requests.RequestException as e:
        probe = None
        probe_error = f"{type(e).__name__}: {e}"
    else:
        probe_error = f"HTTP {probe.status_code}"
    if probe is None or probe.status_code != 200:
        if sogo_container_present():
            result.fail(
                f"opensme-sogo container exists but GET /SOGo/ → {probe_error} "
                "— deployed SOGo must answer (routing/label regression)"
            )
        else:
            result.skip(
                f"SOGo not deployed ({webmail_base}/SOGo/ → {probe_error}, no "
                "opensme-sogo container) — boot the mail profile to run this"
            )
        return result.summary()

    # 1. root redirect → /SOGo/
    # The edge may answer with a scheme upgrade first (entrypoint-level
    # http→https redirect fires before any router matches), so walk at most
    # three hops and require: every hop stays on the webmail host, and the hop
    # that finally lands on /SOGo/ is a PERMANENT redirect.
    url = webmail_base + "/"
    hops: list[tuple[int, str]] = []
    for _ in range(3):
        try:
            r = session.get(url, allow_redirects=False, timeout=T)
        except requests.RequestException:
            result.skip("sogo not running")
            return result.summary()
        loc = r.headers.get("Location", "")
        hops.append((r.status_code, loc))
        if r.status_code not in (301, 302, 303, 307, 308) or not loc:
            break
        url = urljoin(url, loc)
        if url.rstrip("/").endswith("/SOGo"):
            break
    trail = " → ".join(f"{code} {loc[:60]}" for code, loc in hops)
    base_host = urlsplit(webmail_base).netloc
    # A scheme upgrade (http→https) is legitimate; a hop to another host is not.
    off_host = [
        loc for _, loc in hops
        if loc and urlsplit(urljoin(webmail_base + "/", loc)).netloc != base_host
    ]
    landed = hops[-1][1] if hops else ""
    if off_host:
        result.fail(f"GET / → a hop left the webmail host: {off_host[0][:80]}")
    elif not landed.rstrip("/").endswith("/SOGo"):
        result.fail(f"GET / → expected to land on /SOGo/, trail: {trail}")
    elif hops[-1][0] != 301:
        result.fail(f"GET / → the hop to /SOGo/ must be 301 (permanent), got {hops[-1][0]}")
    else:
        via = "" if len(hops) == 1 else f" (via {len(hops) - 1} hop)"
        result.ok(f"root redirects (301) to /SOGo/{via}")

    # 2. login page markup (the precondition probe already fetched it)
    page = probe
    if page.status_code != 200:
        result.fail(f"GET /SOGo/ → {page.status_code} (expected 200)")
        return result.summary()
    body = page.text
    if "SOGo" not in body:
        result.fail("/SOGo/ HTML carries no SOGo marker — is this the right app?")
    elif "<form" not in body and "log" not in body.lower():
        result.fail("/SOGo/ HTML carries no login markup")
    else:
        result.ok("login page serves SOGo markup")

    # 3. every referenced asset must load (THE blank-page regression)
    assets = asset_urls(body, webmail_base)
    if not assets:
        result.fail("login page references no assets — blank-page suspected")
    else:
        broken = []
        for url in assets:
            try:
                ar = session.get(url, timeout=T)
            except requests.RequestException as e:
                broken.append(f"{url} ({e.__class__.__name__})")
                continue
            if ar.status_code != 200:
                broken.append(f"{url} → {ar.status_code}")
        if broken:
            result.fail(f"{len(broken)}/{len(assets)} page assets broken:")
            for b in broken:
                result.info(f"  {b}")
        else:
            result.ok(f"all {len(assets)} referenced assets serve 200")

    # 4. the login backend's own database (page renders from templates, so 1–3
    #    pass even when every SQL query fails — cf. the (null) URL defect)
    tables = sogo_schema_tables()
    if tables == 0:
        result.fail(
            "SOGo created no tables in its database — the SQL layer is dead "
            "(check the URL-encoded password and sogo_user in sogo.conf)"
        )
    elif tables is not None:
        result.ok(f"SOGo owns its database schema ({tables} tables)")

    # 5. a real webmail session: login, then the Angular app's first two calls.
    #    Everything above passes for a stack where no user can log in at all.
    creds = demo_mail_credentials(Path(__file__).resolve().parent.parent.parent)
    rows = sogo_mail_user_rows()
    if creds is None or not all(creds):
        result.skip("no demo user known (bootstrap/demo-users.json missing?)")
    else:
        login, password = creds
        s_session = requests.Session()
        s_session.verify = not localish
        try:
            lr = s_session.post(
                webmail_base + "/SOGo/connect",
                json={"userName": login, "password": password, "rememberLogin": 0},
                timeout=T,
            )
        except requests.RequestException as e:
            lr = None
            login_error = f"{type(e).__name__}: {e}"
        else:
            login_error = f"HTTP {lr.status_code}"
        xsrf = s_session.cookies.get("XSRF-TOKEN", "")
        if lr is None or lr.status_code != 200 or not xsrf:
            # No user provisioned → nothing to judge. Users provisioned → the
            # login is broken, which is a defect (SQL URL, password scheme,
            # missing role in Stalwart).
            if rows:
                result.fail(
                    f"login as {login} → {login_error} (XSRF cookie: "
                    f"{'yes' if xsrf else 'no'}) while sogo.mail_users holds "
                    f"{rows} row(s) — a provisioned user must be able to log in"
                )
            else:
                result.skip(
                    f"login as {login} → {login_error} and no mail user is "
                    "provisioned (scripts/provision-demo-users.sh --component "
                    "stalwart) — nothing to authenticate against"
                )
        else:
            result.ok(f"login as {login} succeeds (session + XSRF cookie)")
            headers = {"Accept": "application/json", "X-XSRF-TOKEN": xsrf}
            identity_email = ""
            actions = [
                (f"/SOGo/so/{login}/Mail/mailAccounts",
                 "account bootstrap (identities)"),
                (f"/SOGo/so/{login}/Mail/0/view", "mailbox view"),
                (f"/SOGo/so/{login}/Mail/0/folderINBOX/view", "INBOX view"),
            ]
            for path, label in actions:
                try:
                    ar = s_session.get(webmail_base + path, headers=headers, timeout=T)
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
                            "— check roles:[\"user\"] on the Stalwart principal "
                            "and SOGoIMAPServer)"
                        )
                        continue
                    emails = [
                        i.get("email", "")
                        for a in accounts if isinstance(a, dict)
                        for i in (a.get("identities") or []) if isinstance(i, dict)
                    ]
                    if not any(login in e for e in emails):
                        result.fail(
                            f"{label} → account list carries no identity for "
                            f"{login} (identities: {emails[:3]})"
                        )
                        continue
                    result.ok(
                        f"{label}: {len(accounts)} mail account(s), "
                        f"identity {emails[0]}"
                    )
                    identity_email = next(e for e in emails if login in e)
                else:
                    result.ok(f"{label} serves 200")

            # 6./7. the mail server behind the webmail: send and receive, with
            #       the same user. Only judgeable once the account bootstrap
            #       named the user's address.
            if not identity_email:
                result.skip(
                    "account bootstrap returned no address for the user — "
                    "submission/delivery not judged"
                )
            else:
                strict = stalwart_container_present()
                subject = submission_send(result, login, identity_email, password, strict)
                if subject:
                    inbox_round_trip(result, login, password, subject, strict)

            # 8. the rest of the groupware: calendar + contacts over DAV. Runs
            #    whenever the login worked — it needs SOGo and its database,
            #    not the IMAP store steps 6/7 depend on.
            dav_groupware(result, webmail_base, not localish, login, password)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
