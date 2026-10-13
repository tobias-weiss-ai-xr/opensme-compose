#!/usr/bin/env python3
"""
tests/05-e2e/mailcow_journey.py — a real mail server delivers real mail
(Epic P).

mailcow-dockerized (git submodule, pinned to an upstream release) is the
openSME stack's full-mail option. This journey exercises it like a user
and an auditor would:

  MA1  the admin UI loads through the openSME Traefik (mail.<domain>)
  MA2  SMTP announces itself on :25 (EHLO → 250, mailcow banner)
  MA3  submission on :587 negotiates STARTTLS
  MA4  IMAPS on :993 negotiates TLS
  MA5  THE delivery round trip: create a domain + mailbox via the REST
      API, authenticate via SMTP submission, mail a message to ourselves,
      and find it in the INBOX over IMAPS — pure stdlib (smtplib/imaplib)
  MA6  SOGo webmail: the groupware UI answers on /SOGo
  MA7  the REST API is NOT reachable through the public edge (auditor)

Requires: the mailcow stack provisioned via `scripts/mailcow.sh up` and
the openSME core running (Traefik routes the UI). Skips cleanly when
mailcow is absent so the main suite stays green everywhere.

Usage:
    python3 tests/05-e2e/mailcow_journey.py [domain]   # default: opensme.local
"""

import argparse
import configparser
import json
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib3
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

import imaplib
import smtplib

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
REPO = Path(__file__).resolve().parent.parent.parent
CONF = REPO / "mail" / "mailcow.conf"


def install_dns_fallback() -> None:
    original = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        try:
            return original(host, *args, **kwargs)
        except socket.gaierror:
            if (isinstance(host, str)
                    and re.fullmatch(r"[a-zA-Z0-9.-]+", host)
                    and any(host.endswith(s) for s in LOCALISH_SUFFIXES)):
                return original("127.0.0.1", *args, **kwargs)
            raise

    socket.getaddrinfo = patched


def read_conf() -> dict[str, str]:
    """Parse the rendered mailcow.conf (KEY=value lines)."""
    conf: dict[str, str] = {}
    for line in CONF.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            conf[k.strip()] = v.strip()
    return conf


def api(session, conf, method: str, path: str, payload=None):
    """Call the mailcow REST API from INSIDE the nginx container (the API
    allowlist covers localhost there — the edge deliberately does not)."""
    cmd = ["docker", "compose",
           "--project-directory", str(REPO / "mail" / "mailcow-dockerized"),
           "--env-file", str(CONF),
           "exec", "-T", "nginx-mailcow", "curl", "-s", "-o", "/dev/stderr",
           "-w", "%{http_code}", "-X", method,
           "-H", f"X-API-Key: {conf['API_KEY']}", "-H", "Content-Type: application/json"]
    if payload is not None:
        cmd += ["-d", json.dumps(payload)]
    cmd += [f"http://localhost:{conf.get('HTTP_PORT', '28080')}/api/v1/{path}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    body = r.stderr
    code = (r.stdout or "").strip()
    return (int(code) if code.isdigit() else 0), body


def tls_probe(host: str, port: int) -> bool:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=T) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return True
    except (OSError, ssl.SSLError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="mailcow delivery journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("mailcow")
    result.header(f"openSME e2e mailcow journey — domain={args.domain}")

    if not CONF.exists():
        result.skip("mailcow not provisioned (scripts/mailcow.sh up)")
        print()
        ok = result.summary()
        return 0 if ok else 1

    ps = subprocess.run(
        ["docker", "compose", "--project-name", "mailcowdockerized",
         "ps", "--services", "--filter", "status=running"],
        capture_output=True, text=True)
    running = set(ps.stdout.split())
    if "nginx-mailcow" not in running or "postfix-mailcow" not in running:
        result.skip("mailcow containers not running")
        print()
        ok = result.summary()
        return 0 if ok else 1

    conf = read_conf()
    mail_host = conf.get("MAILCOW_HOSTNAME", f"mail.{args.domain}")
    session = requests.Session()
    session.verify = False
    session.trust_env = False

    # P1: admin UI through the openSME Traefik
    try:
        ui = session.get(f"https://{mail_host}/", timeout=T)
        (result.ok if ui.status_code == 200 and "mailcow" in ui.text.lower() else result.fail)(
            "mailcow admin UI loads via Traefik" if ui.status_code == 200
            else f"admin UI broken: HTTP {ui.status_code}"
        )
    except requests.RequestException as e:
        result.fail(f"admin UI unreachable: {e.__class__.__name__}")
        print()
        result.summary()
        return 1

    # mailcow's nginx becoming ready does not imply postfix is — retry the
    # first SMTP probe until the daemon answers or the gate expires
    def smtp_ready(port: int) -> bool:
        for _ in range(24):
            try:
                with smtplib.SMTP("127.0.0.1", port, timeout=T) as s:
                    s.ehlo()
                return True
            except (OSError, smtplib.SMTPException):
                time.sleep(5)
        return False

    if not (smtp_ready(25) and smtp_ready(587)):
        result.fail("SMTP daemons never became ready (postfix gate expired)")
        print()
        result.summary()
        return 1

    # P2: SMTP announces on :25
    try:
        with smtplib.SMTP("127.0.0.1", 25, timeout=T) as s:
            code, banner = s.ehlo()
            banner_s = banner.decode(errors="replace")
            (result.ok if code == 250 else result.fail)(
                f"SMTP :25 announces: {banner_s.splitlines()[0][:60]}"
                if code == 250 else f"EHLO refused: {code}"
            )
    except (OSError, smtplib.SMTPException) as e:
        result.fail(f"SMTP :25 unreachable: {e.__class__.__name__}")

    # P3: submission STARTTLS on :587
    try:
        with smtplib.SMTP("127.0.0.1", 587, timeout=T) as s:
            s.ehlo()
            has_tls = s.has_extn("starttls")
            if has_tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                s.starttls(context=ctx)
                s.ehlo()
            (result.ok if has_tls else result.fail)(
                "submission :587 negotiates STARTTLS" if has_tls
                else "submission :587 without STARTTLS!"
            )
    except (OSError, smtplib.SMTPException) as e:
        result.fail(f"submission :587 unreachable: {e.__class__.__name__}")

    # P4: IMAPS TLS on :993
    (result.ok if tls_probe("127.0.0.1", 993) else result.fail)(
        "IMAPS :993 negotiates TLS" if tls_probe("127.0.0.1", 993)
        else "IMAPS :993 TLS handshake failed"
    )

    # P5: the delivery round trip — mailbox in, mail self-addressed, INBOX out
    mailbox = f"e2edemo@{args.domain}"
    # fixed passphrase: provisioning is idempotent (re-runs hit "exists" and
    # keep the original credential, so auth must use the same password)
    pw = "E2e-Delivery-Probe!42"
    code, body = api(session, conf, "POST", "add/domain", {
        "domain": args.domain, "description": "e2e", "aliases": 10,
        "mailboxes": 10, "maxquota": 1024, "quota": 10240, "defquota": 512,
        "restart_sogo": "10", "active": "1",
    })
    domain_ok = code in (200, 201) and ("success" in body or "exists" in body.lower())
    (result.ok if domain_ok else result.fail)(
        "mail domain provisioned via REST API" if domain_ok
        else f"domain provisioning failed: HTTP {code} {body[:80]}"
    )
    code, body = api(session, conf, "POST", "add/mailbox", {
        "local_part": "e2edemo", "domain": args.domain,
        "password": pw, "password2": pw, "quota": 512, "active": "1",
        "name": "E2E Demo Mailbox",
    })
    mailbox_ok = code in (200, 201) and ("success" in body or "exists" in body.lower())
    if mailbox_ok:
        # deterministic credential: re-set the password so the round trip
        # works on fresh stacks AND on re-runs (add keeps the old password)
        code, body = api(session, conf, "POST", "edit/mailbox", {
            "items": [mailbox], "attr": {"password": pw, "password2": pw},
        })
        mailbox_ok = code in (200, 201) and "success" in body
    (result.ok if mailbox_ok else result.fail)(
        "mailbox provisioned via REST API" if mailbox_ok
        else f"mailbox provisioning failed: HTTP {code} {body[:80]}"
    )

    if mailbox_ok:
        delivered = False
        detail = ""
        try:
            msg = EmailMessage()
            msg["From"] = mailbox
            msg["To"] = mailbox
            msg["Subject"] = "openSME e2e delivery probe"
            msg.set_content("If you read this, the mail stack works end to end.")
            with smtplib.SMTP("127.0.0.1", 587, timeout=30) as s:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                s.starttls(context=ctx)
                s.login(mailbox, pw)
                s.send_message(msg)
            # poll the INBOX over IMAPS (delivery is async)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            deadline = time.time() + 90
            while time.time() < deadline and not delivered:
                try:
                    with imaplib.IMAP4_SSL("127.0.0.1", 993, ssl_context=ctx) as im:
                        im.login(mailbox, pw)
                        im.select("INBOX")
                        typ, data = im.search(None, "SUBJECT", '"delivery probe"')
                        delivered = typ == "OK" and bool(data[0].split())
                except (imaplib.IMAP4.error, imaplib.IMAP4.abort, OSError):
                    pass
                if not delivered:
                    time.sleep(5)
            detail = "self-addressed mail found in INBOX (IMAPS)"
        except (smtplib.SMTPException, OSError, imaplib.IMAP4.error) as e:
            detail = f"{e.__class__.__name__}: {e}"
        (result.ok if delivered else result.fail)(
            f"delivery round trip: {detail}" if delivered
            else f"delivery round trip FAILED — {detail}"
        )

    # P6: SOGo webmail answers
    # SOGo is a slow starter — on a cold CI runner the mailcow nginx can 502
    # for the first minute while SOGo initializes (this flaked three pushes
    # in a row before a bounded retry was added). Probe with patience.
    sogo_code = None
    for attempt in range(6):
        try:
            sogo = session.get(f"https://{mail_host}/SOGo/", timeout=T)
            sogo_code = sogo.status_code
            if sogo_code in (200, 302):
                break
        except requests.RequestException as e:
            sogo_code = f"{e.__class__.__name__}"
        time.sleep(10)
    (result.ok if sogo_code in (200, 302) else result.fail)(
        "SOGo webmail answers on /SOGo" if sogo_code in (200, 302)
        else f"SOGo broken after retries: HTTP {sogo_code}"
    )

    # P7: the REST API is not exposed at the edge
    try:
        edge = session.get(f"https://{mail_host}/api/v1/status/version", timeout=T)
        (result.ok if edge.status_code in (401, 403, 404) else result.fail)(
            "REST API correctly not served at the edge" if edge.status_code in (401, 403, 404)
            else f"API exposed at the edge! HTTP {edge.status_code}"
        )
    except requests.RequestException as e:
        result.fail(f"edge API probe failed: {e.__class__.__name__}")

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
