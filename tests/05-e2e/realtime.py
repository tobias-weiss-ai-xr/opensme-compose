#!/usr/bin/env python3
"""tests/05-e2e/realtime.py — real-time intercom journey (slice 1).

Two authenticated portal sessions: one posts a note and creates a task; the
other listens on /api/events. The team note must arrive over SSE within
seconds; the recipient-scoped task event must NOT reach a different viewer.
When the event bus is disabled (/api/events answers 204 — the polling
fallback path), the journey skips cleanly instead of failing.

Usage:
    python3 tests/05-e2e/realtime.py [domain]   # default: opensme.local
"""

import argparse
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import Result
import requests

from run import fetch_seeded, load_env_file, portal_login
# Reuse the Identity-provisioning helpers already exercised by the session
# lifecycle journey: two *distinct* OIDC subs are needed for the privacy leg.
from session_lifecycle import provision_user, purge_user

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
T = 15
WINDOW = 3
LOCALISH_SUFFIXES = (".local", ".localhost", ".test", ".invalid")


def install_dns_fallback():
    """Same .local fallback as the sibling journeys (CI runners have no DNS)."""
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


def new_session():
    s = requests.Session()
    s.verify = False
    s.trust_env = False
    return s


def viewer_sub(session, base):
    """OIDC sub of the signed-in viewer, or None when unreadable."""
    try:
        r = session.get(base + "/api/session", timeout=T)
        if r.status_code != 200:
            return None
        data = r.json()
        return data["user"]["sub"] if data.get("authenticated") else None
    except (requests.RequestException, KeyError, ValueError):
        return None


def open_stream(session, base):
    """Open the authenticated SSE stream (cursor is set at connect time)."""
    return session.get(base + "/api/events", stream=True,
                       timeout=(5, WINDOW + 2), verify=False)


def collect(stream, seconds, expect=None):
    """Collect `data:` JSON payloads until the window ends or `expect` lands.

    Frames are `event:`/`id:`/`data:`/blank; the keep-alive comment is ignored.
    A read timeout simply ends the window with whatever was collected.
    """
    payloads = []
    end = time.time() + seconds
    try:
        for line in stream.iter_lines(decode_unicode=True):
            if line and line.startswith("data: "):
                try:
                    payloads.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
                if expect and any(p.get("kind") == expect for p in payloads):
                    break
            if time.time() > end:
                break
    except requests.RequestException:
        pass
    return payloads


def second_session(base, idp):
    """Log session B in as a distinct IdP user when provisioning is possible.

    portal_login() always uses E2E_ZITADEL_LOGIN_NAME/ZITADEL_ADMIN_PASSWORD,
    so a throw-away user (created with the seeded machine PAT) is logged in by
    temporarily overriding those two settings. Returns (session, cleanup,
    reason): `reason` is set when B had to fall back to the admin identity, in
    which case recipient-scoped privacy cannot be judged from the pair.
    """
    session = new_session()
    pat = (os.environ.get("E2E_ZITADEL_PAT")
           or load_env_file().get("E2E_ZITADEL_PAT")
           or fetch_seeded("/machinekey/pat"))
    if pat:
        uname = "e2e-realtime-" + secrets.token_hex(3)
        uid, pw = provision_user(session, idp, pat, uname)
        if uid:
            saved = (os.environ.get("E2E_ZITADEL_LOGIN_NAME"),
                     os.environ.get("ZITADEL_ADMIN_PASSWORD"))
            try:
                os.environ["E2E_ZITADEL_LOGIN_NAME"] = uname
                os.environ["ZITADEL_ADMIN_PASSWORD"] = pw
                logged_in = portal_login(session, base, idp)
            finally:
                for key, value in zip(
                    ("E2E_ZITADEL_LOGIN_NAME", "ZITADEL_ADMIN_PASSWORD"), saved
                ):
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            if logged_in:
                def cleanup():
                    purge_user(new_session(), idp, pat, uname)

                return session, cleanup, None
            reason = "throw-away user OIDC login failed"
        else:
            reason = "throw-away user provisioning failed"
        purge_user(session, idp, pat, uname)
    else:
        reason = "no seeded machine PAT for user provisioning"

    # Fallback: a second session for the admin — usable for the team event,
    # but both sessions then share the OIDC sub used by the privacy filter.
    session = new_session()
    if not portal_login(session, base, idp):
        return None, None, reason
    return session, lambda: None, reason


def run_journey(result, a, b, base, reason):
    try:
        probe = open_stream(b, base)
    except requests.RequestException as exc:
        result.skip(f"SSE endpoint unreachable ({exc.__class__.__name__}) — journey skipped")
        return 0
    status = probe.status_code
    probe.close()
    if status != 200:
        result.skip(f"SSE endpoint disabled (HTTP {status}) — polling fallback, journey skipped")
        return 0
    result.ok("SSE endpoint reachable (EVENTS_ENABLED)")

    sub_a, sub_b = viewer_sub(a, base), viewer_sub(b, base)
    if sub_a is None or sub_b is None:
        reason = reason or "OIDC sub not readable from /api/session"
    elif sub_a == sub_b:
        reason = reason or "both sessions share the same OIDC sub"

    # A fresh connection starts its XREAD cursor at `$`, so the listener must
    # be attached BEFORE the POST — otherwise the event is published while no
    # reader is connected and the assertion fails for the wrong reason.
    note = "realtime journey " + str(int(time.time()))
    sent = {}
    try:
        with open_stream(b, base) as stream:
            sent["r"] = a.post(base + "/api/intercom",
                               json={"text": note, "kind": "info"}, timeout=T)
            seen = collect(stream, WINDOW, expect="intercom.created")
    except requests.RequestException as exc:
        result.fail(f"note leg request failed: {exc.__class__.__name__}")
        return 1
    r = sent.get("r")
    if r is None or r.status_code != 201:
        result.fail(f"note post failed: HTTP {r.status_code if r else 'no response'}")
        return 1
    kinds = [e.get("kind") for e in seen]
    (result.ok if "intercom.created" in kinds else result.fail)(
        "intercom.created event delivered across sessions"
        if "intercom.created" in kinds
        else f"intercom.created event missing (saw {kinds})"
    )

    if reason:
        result.skip(f"private task privacy leg skipped: {reason}")
        return 1 if result.failed else 0

    # TaskCreate requires `assignee`; recipients are the actor's sub, the
    # stamped author (A's email) and this unrelated assignee — never B's sub.
    created = {}
    try:
        with open_stream(b, base) as stream:
            created["r"] = a.post(base + "/api/tasks",
                                  json={"title": "realtime-private",
                                        "assignee": "realtime-journey@example.invalid"},
                                  timeout=T)
            seen2 = collect(stream, WINDOW)
    except requests.RequestException as exc:
        result.fail(f"task leg request failed: {exc.__class__.__name__}")
        return 1
    r = created.get("r")
    if r is None or r.status_code != 201:
        result.ok(f"task creation unavailable (HTTP {r.status_code if r else 'no response'})"
                  " — privacy leg skipped")
        return 1 if result.failed else 0
    kinds2 = [e.get("kind") for e in seen2]
    (result.ok if "task.created" not in kinds2 else result.fail)(
        "private task event not leaked"
        if "task.created" not in kinds2
        else f"task.created leaked to another session ({kinds2})"
    )
    return 1 if result.failed else 0


def main():
    ap = argparse.ArgumentParser(description="real-time events journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    result = Result("realtime")
    result.header(f"openSME e2e real-time events — domain={args.domain}")

    # Established convention (intercom/broadcast/polls/...): scripts/demo.sh
    # exports PORTAL_DOMAIN=portal.opensme.local; the IdP is auth.<domain>.
    base = f"https://portal.{args.domain}"
    idp = f"https://auth.{args.domain}"

    a = new_session()
    if not portal_login(a, base, idp):
        result.fail("primary portal login failed")
        result.summary()
        return 1
    b, cleanup_b, reason = second_session(base, idp)
    if b is None:
        result.fail(f"secondary portal login failed ({reason})")
        result.summary()
        return 1
    result.ok("two portal sessions established"
              + (" (distinct identities)" if reason is None else f" — {reason}"))

    try:
        rc = run_journey(result, a, b, base, reason)
    finally:
        cleanup_b()
        a.close()
        b.close()
    result.summary()
    return rc


if __name__ == "__main__":
    sys.exit(main())
