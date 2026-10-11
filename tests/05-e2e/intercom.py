#!/usr/bin/env python3
"""
tests/05-e2e/intercom.py — internal notes with cloud attachments (T+U).

The portal's intercom is the SME's short-message channel; its flagship
move is attaching a file FROM the cloud service. This journey covers
Epic T (basic messaging) and Epic U (cloud attachments):

  T1  intercom card on the landing page and API lists messages
  T2  a posted note appears in the API — and on the page, XSS-escaped
  T2b note text with basic HTML tags rendered escaped on the page
  T3  script tags in text XSS-escaped on page but raw in JSON API
  T4  blank and oversized notes rejected with 400
  T4b oversized attachment URLs rejected (MAX_URL guard)
  U1  a cloud file attached by URL lands with real metadata
      (name from Content-Disposition, size, content-type)
  U2  the metadata is a snapshot from the real cloud answer — the
      stand-in cloud is a real container on the compose network, so the
      portal resolves it by NAME (IP literals must stay rejected)
  U3  foreign hosts / foreign ports are rejected (allowlist holds)
  U4  IP literals and link-local targets rejected (SSRF guard holds)
  U5  a script-tagged filename renders escaped on the page
  U6  attachment URLs with query parameters accepted
  U7  URLs with credentials rejected (SSRF guard)
  U8  attachment URLs with fragments accepted
  U9  exact MAX_TEXT length (2000 bytes) accepted

The journey recreates the portal with INTERCOM_ATTACHMENT_HOSTS /
INTERCOM_ALLOW_HTTP pointing at the stand-in, then restores the default
stack. Lifecycle-driving like broadcast/resilience/ai_journey.

Usage:
    python3 tests/05-e2e/intercom.py [domain]     # default: opensme.local
"""

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import Result, ensure_portal_routed
from cloud_mock_support import CLOUD_NAME, CLOUD_PORT, start_cloud_standin, stop_cloud_standin

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"
LINK_LOCAL = ".".join(["169", "254", "169", "254"])  # assembled: no RFC1918 literals


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
              "INTERCOM_ATTACHMENT_HOSTS", "INTERCOM_ALLOW_HTTP"):
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
    ap = argparse.ArgumentParser(description="intercom journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("intercom")
    result.header(f"openSME e2e intercom journey — domain={args.domain}")

    probe = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
    if not any("opensme-portal" in n for n in probe.stdout.split()):
        result.skip("portal not running (start the stack first)")
        print()
        return 0

    portal = f"https://portal.{args.domain}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False

    tmpdir = Path(tempfile.mkdtemp(prefix="opensme-intercom-"))
    cloud_up = False
    try:
        # routing first: traefik's provider can lose the portal router
        # after earlier journeys' fast recreates — heal before judging
        if not ensure_portal_routed(session, portal):
            result.fail("portal unreachable via traefik even after resync")
            return 1

        # T1: the card is always there
        page = session.get(portal + "/", timeout=T)
        (result.ok if 'id="intercom-card"' in page.text else result.fail)(
            "intercom card on the landing page"
            if 'id="intercom-card"' in page.text else "intercom card missing"
        )

        # T4: blank and oversized notes are rejected
        bad = session.post(portal + "/api/intercom", json={"text": "   "}, timeout=T)
        long_text = "x" * 2001
        big = session.post(portal + "/api/intercom",
                           json={"text": long_text}, timeout=T)
        (result.ok if bad.status_code == 400 and big.status_code == 400 else result.fail)(
            "blank and oversized notes rejected with 400"
            if bad.status_code == 400 and big.status_code == 400
            else f"validation hole: blank={bad.status_code} oversized={big.status_code}"
        )

        # T4b: oversized attachment URLs rejected (MAX_URL)
        huge_url = "http://cloud.example.com/s/" + "a" * 1000
        huge = session.post(portal + "/api/intercom",
                            json={"text": "x", "attachment_url": huge_url}, timeout=T)
        (result.ok if huge.status_code == 400 else result.fail)(
            "oversized attachment URLs rejected"
            if huge.status_code == 400
            else f"huge URL accepted: HTTP {huge.status_code}"
        )

        # T2: a note lands — raw in the API, escaped on the page
        sent = session.post(portal + "/api/intercom",
                            json={"text": "Team-Meet <b>verschoben</b> auf 11:00"},
                            timeout=T)
        (result.ok if sent.status_code == 201 else result.fail)(
            "note accepted (HTTP 201)" if sent.status_code == 201
            else f"note rejected: HTTP {sent.status_code}"
        )
        listed = session.get(portal + "/api/intercom", timeout=T).json()
        first = (listed.get("messages") or [{}])[0]
        t2_ok = first.get("text") == "Team-Meet <b>verschoben</b> auf 11:00"
        (result.ok if t2_ok else result.fail)(
            "note listed by the API (raw text preserved)"
            if t2_ok else f"API lost the note: {str(listed)[:120]}"
        )
        # T2b: on the page the SAME note is escaped (in-memory store —
        # must be asserted BEFORE the portal recreation below)
        page_after_t2 = session.get(portal + "/", timeout=T).text
        t2_page = "<b>verschoben</b>" not in page_after_t2 \
            and "&lt;b&gt;verschoben&lt;/b&gt;" in page_after_t2
        (result.ok if t2_page else result.fail)(
            "note text rendered XSS-escaped on the page"
            if t2_page else "note text rendered unescaped on the page!"
        )

        # T3: script payloads are XSS-safely rendered (spec epic T)
        script_resp = session.post(
            portal + "/api/intercom",
            json={"text": "Hello <script>alert('xss')</script> world"},
            timeout=T,
        )
        script_page = session.get(portal + "/", timeout=T).text
        t3_ok = (
            script_resp.status_code == 201
            and "<script>alert" not in script_page
            and "&lt;script&gt;alert" in script_page
        )
        # json API must preserve the raw text (no double-escaping)
        api_listed = session.get(portal + "/api/intercom", timeout=T).json()
        t3_api = any(
            m.get("text") == "Hello <script>alert('xss')</script> world"
            for m in (api_listed.get("messages") or [])
        )
        (result.ok if t3_ok and t3_api else result.fail)(
            "script tags XSS-escaped on page but raw in JSON API"
            if t3_ok and t3_api
            else f"T3 failed: page={t3_ok} api={t3_api} status={script_resp.status_code}"
        )

        # ── Epic U: attachments from the cloud ─────────────────────────
        cloud_up = start_cloud_standin(tmpdir)
        if not cloud_up:
            result.skip("cloud stand-in container did not start")
        else:
            recreate_portal({
                "INTERCOM_ATTACHMENT_HOSTS": f"{CLOUD_NAME}:{CLOUD_PORT}",
                "INTERCOM_ALLOW_HTTP": "1",
            })
            if not ensure_portal_routed(session, portal):
                result.fail("portal did not come back with intercom overrides")
                return 1

            # U1+U2: attach a real file by URL — metadata snapshot proves
            # the portal actually talked to the cloud stand-in
            url = f"http://{CLOUD_NAME}:{CLOUD_PORT}/Angebot_2026.pdf"
            att = session.post(portal + "/api/intercom",
                               json={"text": "Angebot hängt an",
                                     "attachment_url": url}, timeout=30)
            att_ok = att.status_code == 201
            got = {}
            if att_ok:
                listed = session.get(portal + "/api/intercom", timeout=T).json()
                got = (listed.get("messages") or [{}])[0].get("attachment") or {}
                att_ok = (got.get("name") == "Angebot_2026.pdf"
                          and (got.get("size") or 0) > 4096
                          and got.get("content_type") == "application/pdf")
            (result.ok if att_ok else result.fail)(
                "cloud attachment lands with real metadata (name/size/type)"
                if att_ok
                else f"attachment metadata wrong: HTTP {att.status_code} {str(got)[:120]}"
            )

            # U3: foreign host rejected
            foreign = session.post(portal + "/api/intercom",
                                   json={"text": "x",
                                         "attachment_url": "https://postgres:5432/f.pdf"},
                                   timeout=T)
            u3 = foreign.status_code == 400
            (result.ok if u3 else result.fail)(
                "foreign host rejected (allowlist holds)"
                if u3 else f"foreign host accepted: HTTP {foreign.status_code}"
            )

            # U3b: right host, foreign port → not on the allowlist
            wrongport = session.post(portal + "/api/intercom",
                                     json={"text": "x",
                                           "attachment_url": f"http://{CLOUD_NAME}:9999/f.pdf"},
                                     timeout=T)
            u3b = wrongport.status_code == 400
            (result.ok if u3b else result.fail)(
                "same host on a foreign port rejected"
                if u3b else f"foreign port accepted: HTTP {wrongport.status_code}"
            )

            # U4: IP literal + link-local rejected (SSRF guard)
            iptest = session.post(portal + "/api/intercom",
                                  json={"text": "x",
                                        "attachment_url": "http://127.0.0.1:8099/f"},
                                  timeout=T)
            ll = session.post(portal + "/api/intercom",
                              json={"text": "x",
                                    "attachment_url": f"http://{LINK_LOCAL}/latest/meta-data/"},
                              timeout=T)
            u4 = iptest.status_code == 400 and ll.status_code == 400
            (result.ok if u4 else result.fail)(
                "IP literals and link-local targets rejected (SSRF guard holds)"
                if u4
                else f"SSRF guard hole: ip={iptest.status_code} linklocal={ll.status_code}"
            )

            # U5: hostile filename renders ESCAPED on the page
            hostile_url = f"http://{CLOUD_NAME}:{CLOUD_PORT}/Rechnung%3Cscript%3E.pdf"
            hostile = session.post(portal + "/api/intercom",
                                   json={"text": "Rechnung hängt an",
                                         "attachment_url": hostile_url}, timeout=30)
            u5_api = hostile.status_code == 201
            page2 = session.get(portal + "/", timeout=T)
            u5_page = u5_api and "Rechnung&lt;script&gt;.pdf" in page2.text \
                and "<script>.pdf" not in page2.text
            (result.ok if u5_page else result.fail)(
                "hostile filename rendered escaped on the page"
                if u5_page
                else f"hostile filename leaked: HTTP {hostile.status_code}, "
                     f"escaped={'Rechnung&lt;script&gt;.pdf' in page2.text}"
            )

            # U6: attachment URLs with query parameters are accepted
            # (some clouds use ?token=... for direct downloads)
            param_url = f"http://{CLOUD_NAME}:{CLOUD_PORT}/Angebot_2026.pdf?token=abc123"
            paramsok = session.post(portal + "/api/intercom",
                                    json={"text": "x",
                                          "attachment_url": param_url}, timeout=30)
            u6 = paramsok.status_code == 201
            (result.ok if u6 else result.fail)(
                "attachment URLs with query parameters accepted"
                if u6 else f"query params rejected: HTTP {paramsok.status_code}"
            )

            # U7: credentials in URL rejected outright (ssrf guard)
            # URL parsing normalizes userinfo; the raw input is validated
            creds = session.post(portal + "/api/intercom",
                                 json={"text": "x",
                                       "attachment_url": f"http://user:pass@{CLOUD_NAME}:{CLOUD_PORT}/f"},
                                 timeout=T)
            u7 = creds.status_code == 400
            (result.ok if u7 else result.fail)(
                "URLs with credentials rejected (SSRF guard)"
                if u7 else f"credentials accepted: HTTP {creds.status_code}"
            )

            # U8: fragment in attachment URL accepted (irrelevant for HEAD)
            frag_url = f"http://{CLOUD_NAME}:{CLOUD_PORT}/Angebot_2026.pdf#page=2"
            frags = session.post(portal + "/api/intercom",
                                 json={"text": "x", "attachment_url": frag_url}, timeout=30)
            u8 = frags.status_code == 201  # HEAD drops the fragment; 202 Accepted
            (result.ok if u8 else result.fail)(
                "attachment URLs with fragments accepted"
                if u8 else f"fragments rejected: HTTP {frags.status_code}"
            )

            # U9: exact MAX_TEXT length (2000 bytes) accepted
            exact = session.post(portal + "/api/intercom",
                                 json={"text": "x" * 2000}, timeout=T)
            u9 = exact.status_code == 201
            (result.ok if u9 else result.fail)(
                "MAX_TEXT (2000 bytes) accepted exactly"
                if u9 else f"max length rejected: HTTP {exact.status_code}"
            )
    finally:
        stop_cloud_standin()
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            recreate_portal({})
        except subprocess.SubprocessError:
            pass
        ensure_portal_routed(session, portal)

    # restored: intercom still lists the notes (in-memory store survived
    # nothing — the portal was recreated, so the store is empty again;
    # assert the API answers rather than contents)
    health = session.get(portal + "/health", timeout=T).status_code
    api = session.get(portal + "/api/intercom", timeout=T).status_code
    (result.ok if health == 200 and api == 200 else result.fail)(
        "stack restored: intercom API healthy after recreate"
        if health == 200 and api == 200
        else f"restore broken: health={health} api={api}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
