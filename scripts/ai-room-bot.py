#!/usr/bin/env python3
"""scripts/ai-room-bot.py — openSME Matrix room bot ('!ai') via the LiteLLM gateway.

A stdlib-only (urllib, no pip deps) bot that watches the Matrix support room
and answers '!ai <question>' messages by calling the LiteLLM gateway
(opensme-litellm → upstream models, ollama llama3.2:1b as fallback).

Design (verified against the live stack, 2026-11-15):
  * Matrix client API: ONLY /_matrix/client/v3/... — Synapse 1.162.0 on this
    stack rejects the legacy /r0/ prefix (and answers v3; /r3/ does NOT exist).
  * Account: MATRIX_USER (default testuser — the same account
    booking-notify.sh posts with). Booking announcements from that script are
    recognised by their '📅' body prefix and never answered.
  * Loop safety: the bot ignores its own events (sender == login user_id) and
    anything that is not msgtype m.text, so replies never feed back.
  * Send uses PUT .../send/m.room.message/<txnId> with a fresh UUID per
    message (idempotency across retries).
  * Secrets (MATRIX_PASSWORD, LITELLM_API_KEY) are read from the environment
    only and never printed; tokens stay in memory.

Modes:
  --serve          long-poll /sync and answer '!ai <question>' (default loop)
  --ask QUESTION   one-shot gateway answer, print to stdout, exit
  --check          audit: Matrix login + room resolution + gateway reach

Environment (all optional, defaults complete):
  MATRIX_DOMAIN    server_name                       default matrix.home.opensme.graphwiz.ai
  MATRIX_HOMESERVER client API base                  default https://$MATRIX_DOMAIN
  MATRIX_USER      listening account                 default testuser
  MATRIX_PASSWORD  its password                      default $E2E_TESTUSER_PASSWORD then eval2026
  MATRIX_ROOM      '#alias' or '!room_id'            default #support:$MATRIX_DOMAIN
  LITELLM_BASE     gateway base URL                  default http://litellm:4000/v1
  LITELLM_API_KEY  gateway bearer key                default unset → gateway calls fail cleanly
  AI_BOT_MODEL     gateway model id                  default glm-5.3-flash
  AI_BOT_PREFIX    trigger prefix                    default '!ai '
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ── configuration (env → defaults, mirrors booking-notify.sh) ───────────────

MATRIX_DOMAIN = os.environ.get("MATRIX_DOMAIN", "matrix.home.opensme.graphwiz.ai")
MATRIX_HOMESERVER = os.environ.get("MATRIX_HOMESERVER", f"https://{MATRIX_DOMAIN}")
MATRIX_USER = os.environ.get("MATRIX_USER", "testuser")
MATRIX_PASSWORD = os.environ.get("MATRIX_PASSWORD", os.environ.get("E2E_TESTUSER_PASSWORD", "eval2026"))
MATRIX_ROOM = os.environ.get("MATRIX_ROOM", f"#support:{MATRIX_DOMAIN}")
LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm:4000/v1").rstrip("/")
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "")
AI_BOT_MODEL = os.environ.get("AI_BOT_MODEL", "glm-5.3-flash")
AI_BOT_PREFIX = os.environ.get("AI_BOT_PREFIX", "!ai")
TRIGGER = AI_BOT_PREFIX + " "
SYNC_POLL_MS = 29000          # just under the 30s container-healthcheck horizon
GATEWAY_TIMEOUT = 120         # glm-class upstreams answer in <5s; generous budget
SYSTEM_PROMPT = (
    "Du bist ein knapper deutscher KI-Assistent im openSME-Portal "
    "(openSME Muster GmbH). Antworte präzise und in höchstens fünf Sätzen."
)

# booking-notify.sh announces confirmed calrs bookings as the SAME account;
# their bodies always start with this prefix — never answer them.
BOOKING_MARKER = "📅"

_SSL_CTX = ssl.create_default_context()


# ── tiny HTTP helper (JSON in/out, secrets never logged) ────────────────────

def http_json(method: str, url: str, *, bearer: str | None = None,
              body: dict | None = None, timeout: int = 30) -> tuple[int, object]:
    """Return (status, decoded-JSON-or-raw-string). Transport errors raise."""
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    with urllib.request.urlopen(req, data=data, timeout=timeout, context=_SSL_CTX) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return 200, json.loads(raw)
    except json.JSONDecodeError:
        return 200, raw


def http_json_tolerant(method: str, url: str, **kw) -> tuple[int, object]:
    """Like http_json but returns (status, {}) instead of raising on HTTP errors."""
    try:
        return http_json(method, url, **kw)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            payload = {}
        return exc.code, payload


# ── Matrix primitives (v3 only) ─────────────────────────────────────────────

def matrix_login() -> tuple[str, str]:
    """POST /login → (access_token, full user_id)."""
    _, data = http_json("POST", f"{MATRIX_HOMESERVER}/_matrix/client/v3/login", body={
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": MATRIX_USER},
        "password": MATRIX_PASSWORD,
    }, timeout=15)
    token = data.get("access_token") if isinstance(data, dict) else None
    user_id = data.get("user_id") if isinstance(data, dict) else None
    if not token or not user_id:
        raise SystemExit("ai-room-bot: matrix login failed (no access_token) — check MATRIX_PASSWORD")
    return token, user_id


def resolve_room(token: str) -> str:
    """'#alias' → '!room_id' via the directory API; '!id' passes through."""
    if MATRIX_ROOM.startswith("!"):
        return MATRIX_ROOM
    alias = urllib.parse.quote(MATRIX_ROOM, safe="")
    code, data = http_json_tolerant("GET", f"{MATRIX_HOMESERVER}/_matrix/client/v3/directory/room/{alias}", bearer=token)
    room_id = data.get("room_id") if isinstance(data, dict) else None
    if code != 200 or not room_id:
        raise SystemExit(f"ai-room-bot: cannot resolve room alias {MATRIX_ROOM} (HTTP {code})")
    return room_id


def send_text(token: str, room_id: str, text: str) -> None:
    txn = uuid.uuid4().hex
    http_json_tolerant("PUT", f"{MATRIX_HOMESERVER}/_matrix/client/v3/rooms/"
                       f"{urllib.parse.quote(room_id, safe='')}/send/m.room.message/{txn}",
                       bearer=token, body={"msgtype": "m.text", "body": text}, timeout=15)


# ── LiteLLM gateway ─────────────────────────────────────────────────────────

def gateway_chat(question: str) -> str:
    if not LITELLM_API_KEY:
        return "KI-Service ist nicht konfiguriert (LITELLM_API_KEY fehlt)."
    try:
        _, data = http_json("POST", f"{LITELLM_BASE}/chat/completions", bearer=LITELLM_API_KEY, body={
            "model": AI_BOT_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
        }, timeout=GATEWAY_TIMEOUT)
        choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
        answer = (choice.get("message") or {}).get("content", "")
        return answer.strip() or "KI-Service hat ohne Antwort geantwortet."
    except Exception:
        return "KI-Service ist gerade nicht erreichbar — später nochmal versuchen."


# ── modes ───────────────────────────────────────────────────────────────────

def cmd_ask(question: str) -> int:
    print(gateway_chat(question))
    return 0


def cmd_check() -> int:
    problems: list[str] = []
    print(f"matrix:     homeserver={MATRIX_HOMESERVER} user={MATRIX_USER} room={MATRIX_ROOM}")
    print(f"gateway:    base={LITELLM_BASE} model={AI_BOT_MODEL} key={'set' if LITELLM_API_KEY else 'MISSING'}")
    if not LITELLM_API_KEY:
        problems.append("LITELLM_API_KEY not set — gateway answers will be refusal placeholders")
    try:
        code, data = http_json_tolerant("GET", f"{MATRIX_HOMESERVER}/_matrix/client/versions")
        versions = data.get("versions", []) if isinstance(data, dict) else []
        print(f"matrix api: reachable, versions={','.join(versions[:2])}…")
    except Exception as exc:
        problems.append(f"homeserver unreachable: {exc}")
    try:
        token, user_id = matrix_login()
        print(f"matrix login: OK (user_id {user_id})")
        room_id = resolve_room(token)
        print(f"matrix room:  OK ({room_id})")
    except SystemExit as exc:
        problems.append(str(exc))
    gateway_ok = False
    if LITELLM_API_KEY:
        try:
            code, data = http_json_tolerant("GET", f"{LITELLM_BASE}/models", bearer=LITELLM_API_KEY)
            ids = [m.get("id") for m in data.get("data", [])] if isinstance(data, dict) else []
            gateway_ok = code == 200 and bool(ids)
            print(f"gateway:    {'OK' if gateway_ok else 'FAIL'} (HTTP {code}, {len(ids)} models, "
                  f"target {AI_BOT_MODEL} {'present' if AI_BOT_MODEL in ids else 'NOT in catalog'})")
        except Exception as exc:
            problems.append(f"gateway unreachable: {exc}")
    if problems:
        for p in problems:
            print(f"problem:    {p}")
        return 1
    print("check:      OK")
    return 0


def cmd_serve() -> int:
    token, user_id = matrix_login()
    room_id = resolve_room(token)
    print(f"ai-room-bot: serving as {user_id} in {room_id} (model {AI_BOT_MODEL} @ {LITELLM_BASE})", flush=True)

    _, initial = http_json("GET", f"{MATRIX_HOMESERVER}/_matrix/client/v3/sync"
                           f"?timeout=0&filter={{\"room\":{{\"timeline\":{{\"limit\":1}}}}}}", bearer=token, timeout=60)
    since = initial.get("next_batch")
    print("ai-room-bot: caught up with sync, listening for '!ai <question>'", flush=True)

    while True:
        try:
            _, data = http_json("GET", f"{MATRIX_HOMESERVER}/_matrix/client/v3/sync"
                                f"?timeout={SYNC_POLL_MS}&since={urllib.parse.quote(since, safe='')}",
                                bearer=token, timeout=SYNC_POLL_MS // 1000 + 30)
            since = data.get("next_batch", since)
            joined = (data.get("rooms") or {}).get("join") or {}
            for rroom, room in joined.items():
                if rroom != room_id:
                    continue
                for ev in (room.get("timeline") or {}).get("events") or []:
                    handle_event(token, user_id, room_id, ev)
        except KeyboardInterrupt:
            print("ai-room-bot: bye")
            return 0
        except Exception as exc:  # transient network/gateway hiccups — keep serving
            print(f"ai-room-bot: loop error ({exc!r}) — retrying", flush=True)
            time.sleep(3)


def handle_event(token: str, own_user_id: str, room_id: str, ev: dict) -> None:
    if ev.get("type") != "m.room.message":
        return
    content = ev.get("content") or {}
    if content.get("msgtype") != "m.text":
        return
    body = (content.get("body") or "").strip()
    sender = ev.get("sender") or ""
    if not body or sender == own_user_id:      # own loop guard
        return
    if body.startswith(BOOKING_MARKER):        # booking-notify announcements
        return
    question = ""
    if body == AI_BOT_PREFIX:
        question = "Nenne in einem Satz, wofür du gut bist."
    elif body.startswith(TRIGGER):
        question = body[len(TRIGGER):].strip()
    if not question:
        return
    send_text(token, room_id, gateway_chat(question))


def main() -> int:
    parser = argparse.ArgumentParser(description="openSME '!ai' Matrix bot (LiteLLM gateway)")
    parser.add_argument("--serve", action="store_true", help="long-poll sync and answer '!ai <question>'")
    parser.add_argument("--ask", metavar="QUESTION", help="one-shot gateway answer to stdout")
    parser.add_argument("--check", action="store_true", help="audit Matrix login, room resolution, gateway reach")
    args = parser.parse_args()
    if args.ask:
        return cmd_ask(args.ask)
    if args.check:
        return cmd_check()
    return cmd_serve()          # default: serve


if __name__ == "__main__":
    sys.exit(main())
