#!/usr/bin/env python3
"""
tests/05-e2e/polls.py — team polls: create, vote, double-vote, upload (P).

The polls card is OPTIONAL (POLLS_ENABLED gates the whole surface —
store absent → every route 404s). This journey recreates the portal
with polls on, then walks the voting lifecycle end to end through the
real OIDC login, and restores the default stack afterwards.

  P1  login mints a real OPENSMEPORTALSESSION cookie
  P2  polls surface 404-gated without POLLS_ENABLED (authenticated ask —
      the session firewall answers anonymous requests with 401 first,
      so the store-absent 404 is only visible signed in)
  P3  anonymous GET/POST walled out with 401 once enabled
  P4  create → 201, created_by stamped, no voted_by leak, voted=false
  P5  validation: 1 option, 11 options, blank question, oversized
      question (301 bytes) all rejected with 400
  P6  vote → 200, voted=true, votes counted
  P7  double-vote → 409 CONFLICT (per OIDC subject)
  P8  unknown poll id and out-of-range option → 400
  P9  list carries voted=true + tallies for the requesting session,
      still no voted_by subjects
  P10 picked-file upload (local fallback, no cloud configured):
      attachment URL is a /api/files/ link (local fallback route);
      serving carries nosniff + disposition + immutable caching;
      body round-trips
  P11 active-content upload (svg) forced to attachment disposition
  P12 persistence across a portal recreate (POLLS_DATA_PATH volume):
      poll + votes survive
  P13 restored default stack: polls 404 again, intercom still fine

Lifecycle-driving like intercom.py (recreate portal with overrides,
restore afterwards).

Usage:
    python3 tests/05-e2e/polls.py [domain]     # default: opensme.local
"""

import argparse
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import Result, ensure_portal_routed
# Polls are an authenticated surface: reuse run.py's real OIDC login
# helper so this journey carries an OPENSMEPORTALSESSION cookie.
from run import portal_login

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"


def compose_args() -> list[str]:
    import os
    cf = os.environ.get("COMPOSE_FILE") or DEMO_SET
    args: list[str] = []
    for f in cf.split(os.pathsep):
        if f:
            args += ["-f", f]
    return args


def recreate_portal(env_overrides: dict[str, str]) -> None:
    import os
    env = dict(os.environ)
    for k in ("AI_API_URL", "AI_MODEL", "AI_API_KEY",
              "INTERCOM_ATTACHMENT_HOSTS", "INTERCOM_ALLOW_HTTP",
              "POLLS_ENABLED"):
        env.pop(k, None)
    env.update(env_overrides)
    subprocess.run(["docker", "compose"] + compose_args() + ["up", "-d", "portal"],
                   env=env, check=True, capture_output=True, timeout=180)


def wait_portal(session, portal_base, deadline_s=90):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            if session.get(portal_base + "/health", timeout=T).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


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


def main() -> int:
    ap = argparse.ArgumentParser(description="polls journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("polls")
    result.header(f"openSME e2e polls journey — domain={args.domain}")

    probe = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
    running = probe.stdout or ""
    if "opensme-portal" not in running:
        result.fail("portal container not running — start the demo stack first "
                    "(scripts/demo.sh)")
        return 1

    portal = f"https://home.{args.domain}"
    idp_base = f"https://auth.{args.domain}"

    session = requests.Session()
    session.verify = False
    session.trust_env = False

    if not wait_portal(session, portal):
        result.fail("portal unreachable before journey start")
        return 1

    # P1: real OIDC login FIRST — the polls gate (store absent → 404)
    # is only distinguishable from the anon wall (401) when signed in
    if portal_login(session, portal, idp_base):
        result.ok("P1 portal OIDC login minted an OPENSMEPORTALSESSION cookie")
    else:
        result.fail("P1 portal login failed — cannot exercise the polls surface")
        return 1

    # P2: without POLLS_ENABLED the whole surface 404s for a signed-in
    # user (the store is the gate: absent → not-found on every route)
    gated = session.get(portal + "/api/polls", timeout=T)
    p2 = gated.status_code == 404
    if p2:
        landing_off = session.get(portal + "/", timeout=T).text
        if 'id="polls-card"' in landing_off:
            p2 = False
            result.fail("P2 polls card rendered although POLLS_ENABLED is unset")
    (result.ok if p2 else result.fail)(
        "P2 polls 404-gated on the default stack, card not rendered"
        if p2 else f"P2 expected 404 without POLLS_ENABLED, got {gated.status_code}"
    )

    # Recreate the portal with polls on (default data path → /data/polls.json
    # on the portal-data volume → P12 persistence has something to bite on)
    recreate_portal({"POLLS_ENABLED": "1"})
    if not ensure_portal_routed(session, portal):
        result.fail("portal did not come back with POLLS_ENABLED=1")
        return 1

    # P3: anonymous wall — polls sit behind the session firewall
    anon = requests.Session()
    anon.verify = False
    anon.trust_env = False
    a_get = anon.get(portal + "/api/polls", timeout=T)
    a_post = anon.post(portal + "/api/polls",
                       json={"question": "x", "options": ["a", "b"]}, timeout=T)
    landing_on = session.get(portal + "/", timeout=T).text
    p3 = (a_get.status_code == 401 and a_post.status_code == 401
          and 'id="polls-card"' in landing_on and 'id="tab-umfragen"' in landing_on)
    (result.ok if p3 else result.fail)(
        "P3 anonymous polls GET/POST walled out with 401; card + tab rendered when enabled"
        if p3
        else f"P3 not gated/cardless: GET={a_get.status_code} POST={a_post.status_code} "
             f"card={'polls-card' in landing_on}"
    )

    # P4: create a poll — author stamp, no subject leak, fresh state
    created = session.post(portal + "/api/polls",
                           json={"question": "Journey: Anzug fürs Team-Foto?",
                                 "options": ["Hemd", "T-Shirt"]},
                           timeout=T)
    poll = (created.json() or {}).get("poll", {}) if created.status_code == 201 else {}
    p4 = [
        created.status_code == 201,
        bool(poll.get("created_by")),
        "voted_by" not in poll,
        poll.get("voted") is False,
        poll.get("votes") == [0, 0],
    ]
    (result.ok if all(p4) else result.fail)(
        "P4 create → 201, created_by stamped, voted=false, no voted_by"
        if all(p4)
        else f"P4 create defect: HTTP {created.status_code}, poll={poll}"
    )
    pid = poll.get("id")

    # P5: validation corner cases
    cases = [
        ("too few options", {"question": "q", "options": ["only"]}),
        ("too many options", {"question": "q", "options": [f"o{i}" for i in range(11)]}),
        ("blank question", {"question": "   ", "options": ["a", "b"]}),
        ("oversized question", {"question": "x" * 301, "options": ["a", "b"]}),
    ]
    codes = []
    for name, payload in cases:
        r = session.post(portal + "/api/polls", json=payload, timeout=T)
        codes.append((name, r.status_code))
    p5 = all(c == 400 for _, c in codes)
    (result.ok if p5 else result.fail)(
        "P5 invalid polls (options 1/11, blank, 301-byte question) → 400"
        if p5 else f"P5 validation hole: {codes}"
    )

    # P6: vote lands, response is server-truth
    voted = session.post(portal + f"/api/polls/{pid}/vote",
                         json={"option": 0}, timeout=T)
    v_poll = (voted.json() or {}).get("poll", {}) if voted.status_code == 200 else {}
    p6 = [
        voted.status_code == 200,
        v_poll.get("voted") is True,
        v_poll.get("votes") == [1, 0],
    ]
    (result.ok if p6 else result.fail)(
        "P6 vote → 200, voted=true, votes=[1,0]"
        if p6 else f"P6 vote defect: HTTP {voted.status_code}, poll={v_poll}"
    )

    # P7: double-vote is a CONFLICT (per OIDC subject)
    again = session.post(portal + f"/api/polls/{pid}/vote",
                         json={"option": 1}, timeout=T)
    p7 = again.status_code == 409
    (result.ok if p7 else result.fail)(
        "P7 double-vote → 409 CONFLICT"
        if p7 else f"P7 double-vote got HTTP {again.status_code}"
    )

    # P8: unknown poll / bad option index
    ghost = session.post(portal + "/api/polls/999999/vote",
                         json={"option": 0}, timeout=T)
    bad_idx = session.post(portal + f"/api/polls/{pid}/vote",
                           json={"option": 99}, timeout=T)
    p8 = ghost.status_code == 400 and bad_idx.status_code == 400
    (result.ok if p8 else result.fail)(
        "P8 unknown poll id and out-of-range option → 400"
        if p8 else f"P8 vote guard: ghost={ghost.status_code} idx={bad_idx.status_code}"
    )

    # P9: list reflects the session (voted flag) without leaking subjects
    listed = session.get(portal + "/api/polls", timeout=T)
    polls = (listed.json() or {}).get("polls", []) if listed.status_code == 200 else []
    mine = next((p for p in polls if p.get("id") == pid), {})
    p9 = [
        listed.status_code == 200,
        bool(mine),
        mine.get("voted") is True,
        mine.get("votes") == [1, 0],
        all("voted_by" not in p for p in polls),
    ]
    (result.ok if p9 else result.fail)(
        "P9 list: voted=true + tallies for the requester, no voted_by anywhere"
        if p9 else f"P9 list defect: HTTP {listed.status_code}, mine={mine}"
    )

    # P10: picked-file upload — local fallback (no CLOUD_WEBDAV configured),
    # hardened serving contract (same headers the intercom files carry)
    import json as _json
    up = session.post(portal + "/api/polls/upload",
                      params={"question": "Journey: Regeln als Datei?",
                              "options": _json.dumps(["ja", "nein"]),
                              "filename": "regeln.txt"},
                      data=b"regelwerk-v1", timeout=T)
    upoll = (up.json() or {}).get("poll", {}) if up.status_code == 201 else {}
    uatt = upoll.get("attachment") or {}
    uurl = uatt.get("url", "")
    served = session.get(portal + uurl, timeout=T) if uurl.startswith("/api/") else None
    p10 = [
        up.status_code == 201,
        upoll.get("id") != pid,  # a NEW poll, not a vote on the old one
        bool(upoll.get("created_by")),
        uurl.startswith("/api/files/"),
        uatt.get("size") == len(b"regelwerk-v1"),
        served is not None and served.status_code == 200,
        served is not None and served.content == b"regelwerk-v1",
        served is not None
        and served.headers.get("X-Content-Type-Options") == "nosniff",
        served is not None
        and served.headers.get("Content-Disposition") == 'inline; filename="regeln.txt"',
        served is not None
        and "immutable" in served.headers.get("Cache-Control", ""),
    ]
    (result.ok if p10 else result.fail)(
        "P10 upload → local /api/files/ link, nosniff+inline+immutable, body ok"
        if p10 else f"P10 upload defect: HTTP {up.status_code}, att={uatt}"
    )

    # P11: active content never inline
    up_svg = session.post(portal + "/api/polls/upload",
                          params={"question": "Journey: SVG verlinken?",
                                  "options": _json.dumps(["ja", "nein"]),
                                  "filename": "logo.svg"},
                          data=b"<svg/>", timeout=T)
    satt = ((up_svg.json() or {}).get("poll", {}) or {}).get("attachment") or {}
    surl = satt.get("url", "")
    s_served = session.get(portal + surl, timeout=T) if surl.startswith("/api/") else None
    p11 = (
        up_svg.status_code == 201
        and s_served is not None
        and s_served.status_code == 200
        and s_served.headers.get("Content-Disposition", "").startswith("attachment")
    )
    (result.ok if p11 else result.fail)(
        "P11 svg upload forced to attachment disposition"
        if p11 else f"P11 svg serving not hardened: {surl}"
    )

    # P12: persistence — recreate the portal (same env), votes survive
    recreate_portal({"POLLS_ENABLED": "1"})
    if not ensure_portal_routed(session, portal):
        result.fail("P12 portal did not come back after recreate")
        return 1
    relisted = session.get(portal + "/api/polls", timeout=T)
    rpolls = (relisted.json() or {}).get("polls", []) if relisted.status_code == 200 else []
    rmine = next((p for p in rpolls if p.get("id") == pid), {})
    p12 = [
        relisted.status_code == 200,
        bool(rmine),
        rmine.get("votes") == [1, 0],
        rmine.get("voted") is True,
    ]
    (result.ok if p12 else result.fail)(
        "P12 poll + votes survive a portal recreate (POLLS_DATA_PATH volume)"
        if p12 else f"P12 persistence defect: HTTP {relisted.status_code}, poll={rmine}"
    )

    # P13: restore the default stack — polls gated off again, intercom fine
    try:
        recreate_portal({})
    except subprocess.SubprocessError:
        pass
    ensure_portal_routed(session, portal)
    off = session.get(portal + "/api/polls", timeout=T)
    ic = session.get(portal + "/api/intercom", timeout=T)
    health = session.get(portal + "/health", timeout=T)
    p13 = off.status_code == 404 and ic.status_code == 200 and health.status_code == 200
    (result.ok if p13 else result.fail)(
        "P13 restored: polls 404 again, intercom + health intact"
        if p13 else f"P13 restore defect: polls={off.status_code} ic={ic.status_code}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
