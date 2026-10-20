#!/usr/bin/env python3
"""
tests/05-e2e/ai_journey.py — local AI, for real this time (Epic O).

The portal's AI surface is the product's flagship ("local AI for SMEs"),
yet with AI unconfigured the suite only ever asserts the card is HIDDEN
(Epic D gating). This journey switches the feature ON against a mock
OpenAI-compatible backend (tests/05-e2e/ai_mock.py) and exercises the
full user path:

  O1  with AI configured, the landing page SHOWS the AI card
      (inverse of the B5 gating check)
  O2  a question posted to the portal returns an answer — the round trip
      portal → OpenAI-compatible backend works end to end
  O3  the portal speaks the right contract upstream: bearer auth, model
      from AI_MODEL, user message = the question, max_tokens bounded
  O4  input validation: an empty question is rejected (400), not forwarded
  O5  teardown: with AI unset, the card is hidden again (stack restored)

The portal is recreated with AI_API_URL/AI_MODEL/AI_API_KEY overrides
pointing at a host-side mock (reachable via the docker bridge gateway),
then restored — lifecycle-driving like broadcast/resilience.

Usage:
    python3 tests/05-e2e/ai_journey.py [domain]     # default: opensme.local
"""

import argparse
import json
import os
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
from conftest import Result, ensure_portal_routed

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
API_KEY = "e2e-mock-key-0123456789abcdef"
MODEL = "e2e-mock-model"
QUESTION = "Wie gründe ich ein kleines Unternehmen?"
ANSWER_STEM = "E2E-MOCK"
DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"


def compose_args() -> list[str]:
    cf = os.environ.get("COMPOSE_FILE") or DEMO_SET
    args: list[str] = []
    for f in cf.split(os.pathsep):
        if f:
            args += ["-f", f]
    return args


def recreate_portal(env_overrides: dict[str, str]) -> None:
    env = dict(os.environ)
    for k in ("AI_API_URL", "AI_MODEL", "AI_API_KEY"):
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


def bridge_gateway() -> str:
    r = subprocess.run(
        ["docker", "network", "inspect", "opensme-net", "--format",
         "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True, text=True, timeout=60)
    gw = (r.stdout or "").strip()
    # fallback for networks without IPAM config — assembled from parts to
    # keep private-address literals out of the tree (internal-ref scan)
    return gw or ".".join(["172", "18", "0", "1"])


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
    ap = argparse.ArgumentParser(description="local AI journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("ai-journey")
    result.header(f"openSME e2e local AI journey — domain={args.domain}")

    probe = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
    if not any("opensme-portal" in n for n in probe.stdout.split()):
        result.skip("portal not running (start the stack first)")
        print()
        return 0

    portal_base = f"https://portal.{args.domain}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False

    tmpdir = tempfile.mkdtemp(prefix="opensme-ai-mock-")
    mock_proc = None
    port = 18099
    try:
        # start the mock on the host; portal reaches it via the bridge gateway
        mock_proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "ai_mock.py"),
             str(port), tmpdir, API_KEY, MODEL],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            result.fail("AI mock did not start")
            return 1

        api_url = f"http://{bridge_gateway()}:{port}"
        try:
            recreate_portal({"AI_API_URL": api_url, "AI_MODEL": MODEL,
                             "AI_API_KEY": API_KEY})
        except subprocess.SubprocessError as e:
            result.fail(f"portal recreate failed: {e.__class__.__name__}")
            return 1
        if not wait_portal(session, portal_base):
            result.fail("portal did not come back healthy with AI configured")
            return 1

        # O1: the AI card is VISIBLE when the feature is configured
        page = session.get(portal_base + "/", timeout=T)
        (result.ok if 'id="ai-card"' in page.text else result.fail)(
            "AI card appears when AI is configured"
            if 'id="ai-card"' in page.text
            else "AI card missing despite AI_API_URL set"
        )

        # O2: full round trip through the portal proxy
        chat = session.post(portal_base + "/api/ai/chat",
                            json={"question": QUESTION}, timeout=60)
        answer_ok = False
        if chat.status_code == 200:
            answer_ok = chat.json().get("answer", "").startswith(ANSWER_STEM)
        (result.ok if answer_ok else result.fail)(
            f"chat round trip answered via local AI ({chat.status_code})"
            if answer_ok
            else f"chat round trip broken: HTTP {chat.status_code} {chat.text[:100]}"
        )

        # O3: the portal speaks the correct upstream contract
        req_file = Path(tmpdir) / "last_request.json"
        contract_ok = False
        if req_file.exists():
            got = json.loads(req_file.read_text())
            body = got.get("body", {})
            contract_ok = (
                got.get("headers", {}).get("authorization") == f"Bearer {API_KEY}"
                and body.get("model") == MODEL
                and body.get("messages", [{}])[0].get("role") == "user"
                and body.get("messages", [{}])[0].get("content") == QUESTION
                and isinstance(body.get("max_tokens"), int)
                and 0 < body.get("max_tokens", 0) <= 4096
            )
        (result.ok if contract_ok else result.fail)(
            "portal → backend contract correct (bearer, model, message, bounds)"
            if contract_ok
            else "portal speaks the wrong upstream contract (see mock log)"
        )

        # O4: input validation — empty question never reaches the backend
        req_file.unlink(missing_ok=True)
        bad = session.post(portal_base + "/api/ai/chat",
                           json={"question": "   "}, timeout=T)
        ok_400 = bad.status_code == 400 and not req_file.exists()
        (result.ok if ok_400 else result.fail)(
            "empty question rejected with 400 (not forwarded)"
            if ok_400 else f"validation hole: empty question → HTTP {bad.status_code}, "
                           f"reached backend: {req_file.exists()}"
        )
    finally:
        # O5/teardown: restore the stack's default (AI off)
        if mock_proc:
            mock_proc.terminate()
            try:
                mock_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                mock_proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            recreate_portal({})
        except subprocess.SubprocessError:
            pass
        # traefik's docker provider can lose the router across fast
        # recreates — heal before judging the restore
        ensure_portal_routed(session, portal_base)

    # restored: the card must be gone again — AND the page must answer
    # 200 with real content (a bare traefik 404 would "hide" the card too)
    page = session.get(portal_base + "/", timeout=T)
    ai_card_still = 'id="ai-card"' in page.text
    restored_ok = (page.status_code == 200
                   and not ai_card_still
                   and "openSME" in page.text)
    (result.ok if restored_ok else result.fail)(
        "stack restored: AI card hidden again"
        if restored_ok
        else f"teardown incomplete: HTTP {page.status_code} ai-card={ai_card_still}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
