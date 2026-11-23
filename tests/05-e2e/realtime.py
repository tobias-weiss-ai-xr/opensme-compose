#!/usr/bin/env python3
"""tests/05-e2e/realtime.py — real-time intercom journey (slice 1).

Two authenticated portal sessions with DISTINCT OIDC identities: one posts a
note and creates a task; the other listens on /api/events. The team note must
arrive over SSE within seconds; the recipient-scoped task event must NOT reach
the other viewer. Only a disabled/unreachable stream (HTTP 204 / transport
error) skips cleanly: with SSE enabled, an inability to establish two distinct
identities or to run the privacy assertion is a hard failure.

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


def safe_cleanup(result, cleanup, what):
    """Run a cleanup step without ever letting it escape the Result-based rc."""
    try:
        cleanup()
    except Exception as exc:
        result.warn(f"{what} cleanup failed ({exc.__class__.__name__}: {exc})")


def safe_close(result, session, what):
    try:
        session.close()
    except Exception as exc:
        result.warn(f"{what} close failed ({exc.__class__.__name__}: {exc})")


def restore_credentials(result, saved):
    """Undo the temporary OIDC login override, one key at a time."""
    for key, value in zip(
        ("E2E_ZITADEL_LOGIN_NAME", "ZITADEL_ADMIN_PASSWORD"), saved
    ):
        try:
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        except Exception as exc:
            result.warn(f"could not restore {key} ({exc.__class__.__name__}: {exc})")


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


def has_kind(payloads, kind, marker=None):
    """True when a collected payload is `kind` (and carries `marker`, if set)."""
    return any(
        p.get("kind") == kind
        and (marker is None or marker in json.dumps(p))
        for p in payloads
    )


def wait_for_ready(stream, seconds):
    """Consume frames until the backend's `ready` proves XREAD is live.

    `ready` is emitted only after the cursor check is conclusive and a batch
    read succeeded. Keep-alive comments are also sent on read-error paths, so
    they are NOT a valid liveness proof for the delivery assertions below.
    """
    end = time.time() + seconds
    try:
        for line in stream.iter_lines(decode_unicode=True):
            if line == "event: ready":
                return True
            if time.time() > end:
                break
    except requests.RequestException:
        pass
    return False


def collect(stream, seconds, expect=None, marker=None):
    """Collect `data:` JSON payloads until the window ends or `expect` lands.

    Frames are `event:`/`id:`/`data:`/blank; the keep-alive comment is ignored.
    A read timeout simply ends the window with whatever was collected. `marker`
    makes the short-circuit wait for a SPECIFIC event, not just its kind.
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
                if expect and has_kind(payloads, expect, marker):
                    break
            if time.time() > end:
                break
    except requests.RequestException:
        pass
    return payloads


def second_session(result, base, idp):
    """Log session B in as a DISTINCT throw-away IdP user.

    portal_login() always reads E2E_ZITADEL_LOGIN_NAME/ZITADEL_ADMIN_PASSWORD,
    so the throw-away user (created with the seeded machine PAT) is logged in
    by temporarily overriding those two settings. Returns (session, cleanup,
    error); `error` is None only when B really is a different identity. There
    is deliberately NO shared-admin fallback: with SSE enabled the privacy
    assertion is mandatory, so a setup failure must fail the journey. The
    caller owns `cleanup` and must run it through safe_cleanup().
    """
    session = new_session()

    def noop():
        pass

    try:
        pat = (os.environ.get("E2E_ZITADEL_PAT")
               or load_env_file().get("E2E_ZITADEL_PAT")
               or fetch_seeded("/machinekey/pat"))
    except Exception as exc:
        return None, noop, f"machine PAT lookup failed ({exc.__class__.__name__})"
    if not pat:
        return None, noop, "no seeded machine PAT for user provisioning"

    uname = "e2e-realtime-" + secrets.token_hex(3)
    try:
        uid, pw = provision_user(session, idp, pat, uname)
    except Exception as exc:
        return None, noop, f"user provisioning failed ({exc.__class__.__name__}: {exc})"
    if not uid:
        return None, noop, "user provisioning returned no user id"

    def cleanup():
        purge_user(new_session(), idp, pat, uname)

    saved = (os.environ.get("E2E_ZITADEL_LOGIN_NAME"),
             os.environ.get("ZITADEL_ADMIN_PASSWORD"))
    try:
        os.environ["E2E_ZITADEL_LOGIN_NAME"] = uname
        os.environ["ZITADEL_ADMIN_PASSWORD"] = pw
    except Exception as exc:
        result.warn(f"login override failed ({exc.__class__.__name__}: {exc})")
        restore_credentials(result, saved)
        safe_cleanup(result, cleanup, "throw-away IdP user")
        return None, noop, "could not override login credentials"

    try:
        logged_in = portal_login(session, base, idp)
    except Exception as exc:
        logged_in = False
        result.warn(f"distinct user login raised {exc.__class__.__name__}: {exc}")
    finally:
        restore_credentials(result, saved)

    if not logged_in:
        safe_cleanup(result, cleanup, "throw-away IdP user")
        return None, noop, "throw-away user OIDC login failed"
    return session, cleanup, None


def run_journey(result, a, b, base):
    try:
        probe = open_stream(b, base)
    except requests.RequestException as exc:
        result.skip(f"SSE endpoint unreachable ({exc.__class__.__name__}) — journey skipped")
        return 0
    status = probe.status_code
    probe.close()
    if status == 204:
        result.skip("SSE endpoint disabled (HTTP 204) — polling fallback, journey skipped")
        return 0
    if status != 200:
        # CI sets EVENTS_ENABLED=1 for this job, so anything other than 204 is
        # a proxy/auth/cap regression, not an intentionally disabled feature.
        result.fail(f"SSE endpoint returned HTTP {status}; expected 200 with EVENTS_ENABLED=1")
        return 1
    result.ok("SSE endpoint reachable (EVENTS_ENABLED)")

    # The privacy filter compares OIDC subs. Without two readable, different
    # subs the task leg cannot prove anything — fail, never skip.
    sub_a, sub_b = viewer_sub(a, base), viewer_sub(b, base)
    if sub_a is None or sub_b is None:
        result.fail(f"OIDC sub not readable from /api/session (A={sub_a!r}, B={sub_b!r})")
        return 1
    if sub_a == sub_b:
        result.fail(f"both portal sessions share the same OIDC sub ({sub_a!r})")
        return 1
    result.ok("two distinct OIDC identities confirmed")

    # A fresh connection starts its XREAD cursor at `$`, so the listener must
    # be attached BEFORE the POST — otherwise the event is published while no
    # reader is connected and the assertion fails for the wrong reason.
    note = "realtime journey " + str(int(time.time()))
    sent = {}
    try:
        with open_stream(b, base) as stream:
            if stream.status_code != 200:
                result.fail(f"note leg SSE stream HTTP {stream.status_code} — delivery assertion cannot run")
                return 1
            if not wait_for_ready(stream, WINDOW):
                result.fail("note leg SSE reader never became live — delivery assertion cannot run")
                return 1
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

    # Prove THIS stream is live before trusting the absence of task.created:
    # a witness Team event from A must reach B first. A dead/non-200 stream
    # would otherwise make "no leak" pass vacuously.
    marker = "sse-liveness-" + secrets.token_hex(6)
    control = {}
    created = {}
    actor_seen = []
    try:
        with open_stream(b, base) as stream, open_stream(a, base) as actor_stream:
            if stream.status_code != 200 or actor_stream.status_code != 200:
                result.fail(f"task leg SSE streams not healthy (B={stream.status_code}, "
                            f"A={actor_stream.status_code}) — privacy assertion cannot run")
                return 1
            if not wait_for_ready(stream, WINDOW) or not wait_for_ready(actor_stream, WINDOW):
                result.fail("task leg SSE readers never became live — privacy assertion cannot run")
                return 1
            control["r"] = a.post(base + "/api/intercom",
                                  json={"text": "control " + marker, "kind": "info"},
                                  timeout=T)
            if control["r"].status_code != 201:
                result.fail(f"liveness control note failed: HTTP {control['r'].status_code}")
                return 1
            control_seen = collect(stream, WINDOW,
                                   expect="intercom.created", marker=marker)
            if not has_kind(control_seen, "intercom.created", marker):
                kinds_seen = [e.get("kind") for e in control_seen]
                result.fail(f"SSE liveness control not received on session B (saw {kinds_seen})")
                return 1
            result.ok("SSE liveness control delivered before privacy assertion")

            # TaskCreate requires `assignee`; recipients are the actor's sub,
            # the stamped author (A's email) and this unrelated assignee —
            # never B's identity.
            created["r"] = a.post(base + "/api/tasks",
                                  json={"title": "realtime-private",
                                        "assignee": "realtime-journey@example.invalid"},
                                  timeout=T)
            actor_seen = collect(actor_stream, WINDOW, expect="task.created")
            seen2 = collect(stream, WINDOW)
    except requests.RequestException as exc:
        result.fail(f"task leg request failed: {exc.__class__.__name__}")
        return 1
    r = created.get("r")
    if r is None or r.status_code != 201:
        result.fail(f"task creation failed (HTTP {r.status_code if r else 'no response'})"
                    " — privacy assertion cannot run")
        return 1
    # A silent XADD failure would make "not leaked" pass vacuously — there
    # would be no task event to filter at all.
    if not has_kind(actor_seen, "task.created"):
        result.fail(f"actor A did not receive its own task.created (saw "
                    f"{[e.get('kind') for e in actor_seen]}) — privacy assertion would be vacuous")
        return 1
    result.ok("actor receives own task.created before privacy assertion")
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
    try:
        primary_ok = portal_login(a, base, idp)
    except Exception as exc:
        primary_ok = False
        result.warn(f"primary portal login raised {exc.__class__.__name__}: {exc}")
    if not primary_ok:
        result.fail("primary portal login failed")
        safe_close(result, a, "primary session")
        result.summary()
        return 1

    b, cleanup_b, setup_error = second_session(result, base, idp)
    if b is None:
        result.fail(f"distinct second session unavailable: {setup_error}")
        safe_cleanup(result, cleanup_b, "throw-away IdP user")
        safe_close(result, a, "primary session")
        result.summary()
        return 1
    result.ok("two portal sessions established (distinct identities)")

    try:
        rc = run_journey(result, a, b, base)
    except Exception as exc:
        result.fail(f"journey error: {exc.__class__.__name__}: {exc}")
        rc = 1
    finally:
        safe_cleanup(result, cleanup_b, "throw-away IdP user")
        safe_close(result, a, "primary session")
        safe_close(result, b, "secondary session")
    result.summary()
    return rc


if __name__ == "__main__":
    sys.exit(main())
