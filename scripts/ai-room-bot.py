#!/usr/bin/env python3
"""scripts/ai-room-bot.py — openSME Matrix room bot ('!ai') via the LiteLLM gateway.

A stdlib-only (urllib, no pip deps) bot that watches Matrix rooms and answers
'!ai <question>' messages by calling the LiteLLM gateway
(opensme-litellm → upstream models, ollama llama3.2:1b as fallback).

Beyond the configured start room (MATRIX_ROOM, default #support) the bot
ACCEPTS INVITES: a pending m.room.member membership=invite for its own
account is answered with POST /_matrix/client/v3/join/<roomId> and the room
is added to the served set — !ai/!entwurf/!brief then work there too.

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

Room commands (all answered in-room, German):
  !ai <question>   LLM answer via the LiteLLM gateway
  !entwurf <empfänger>: <stichpunkte>
                   LLM composes a German reply draft → RFC-5322 message is
                   APPENDed to the bot account's Drafts mailbox with the
                   \\Draft flag → shows up in SOGo under „Entwürfe"; the
                   user reviews and sends it there. Zero SOGo modification.
  !brief           inbox brief posted back: unread count + newest items
                   (headers only, BODY.PEEK — nothing is marked read)

Modes:
  --serve          long-poll /sync and answer the room commands (default loop)
  --ask QUESTION   one-shot gateway answer, print to stdout, exit
  --check          audit: Matrix login + room resolution + gateway reach + IMAP

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
  IMAP_HOST        Stalwart IMAP host                default stalwart (published 143:143 on host)
  IMAP_PORT        its port                          default 143
  IMAP_MAIL_DOMAIN mail domain of the bot account    default opensme.local
  IMAP_USER        IMAP login                        default <MATRIX_USER>@<IMAP_MAIL_DOMAIN>
                                                     (used as-is when it already contains '@')
  IMAP_PASSWORD    Stalwart APP password for IMAP    default unset → falls back to MATRIX_PASSWORD.
                                                     NOTE: the OIDC-backed directory REJECTS the
                                                     normal account password for PLAIN/LOGIN — seed
                                                     an app password once via
                                                     scripts/stalwart-app-password.sh and pass it here.
  IMAP_DRAFTS      Drafts mailbox name               default Drafts (SOGo label: „Entwürfe")
  AI_BRIEF_LIMIT   '!brief' newest-items limit       default 8
"""

from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import imaplib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage

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
ENTWURF_CMD = "!entwurf"      # '!entwurf <empfänger>: <stichpunkte>'
BRIEF_CMD = "!brief"          # '!brief' → inbox brief in the room
SYNC_POLL_MS = 29000          # just under the 30s container-healthcheck horizon
GATEWAY_TIMEOUT = 120         # glm-class upstreams answer in <5s; generous budget
SYSTEM_PROMPT = (
    "Du bist ein knapper deutscher KI-Assistent im openSME-Portal "
    "(openSME Muster GmbH). Antworte präzise und in höchstens fünf Sätzen."
)
DRAFT_SYSTEM_PROMPT = (
    "Du bist Assistenz der openSME Muster GmbH und verfasst kurze, sachliche, "
    "höfliche deutsche geschäftliche E-Mails. Gib in der ERSTEN Zeile exakt "
    "'Betreff: ' gefolgt von einem knappen Betreff aus, danach eine Leerzeile, "
    "danach den E-Mail-Text mit passender Anrede, 2-4 kurzen Absätzen und "
    "Grußformel. Kein Markdown, keine Platzhalter, keine erfundenen Fakten."
)

# booking-notify.sh announces confirmed calrs bookings as the SAME account;
# their bodies always start with this prefix — never answer them.
BOOKING_MARKER = "📅"

# ── IMAP handoff (Stalwart) — drafts land in SOGo under „Entwürfe" ──────────
# Verified against the live stack (2026-11-15): Stalwart's directory is the
# OIDC bridge — PLAIN/LOGIN accepts ONLY app passwords (webmail itself uses
# XOAUTH2). The normal account password (MATRIX_PASSWORD) is therefore only a
# last-resort fallback that will fail with AUTHENTICATIONFAILED; seed an app
# password via scripts/stalwart-app-password.sh and pass IMAP_PASSWORD.
IMAP_HOST = os.environ.get("IMAP_HOST", "stalwart")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "143"))
IMAP_MAIL_DOMAIN = os.environ.get("IMAP_MAIL_DOMAIN", "opensme.local")
_default_imap_user = (
    MATRIX_USER if "@" in MATRIX_USER else f"{MATRIX_USER}@{IMAP_MAIL_DOMAIN}"
)
IMAP_USER = os.environ.get("IMAP_USER", _default_imap_user)
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
IMAP_DRAFTS = os.environ.get("IMAP_DRAFTS", "Drafts")   # SOGo label: „Entwürfe"
AI_BRIEF_LIMIT = int(os.environ.get("AI_BRIEF_LIMIT", "8"))
IMAP_TIMEOUT = 15

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


def join_room(token: str, room_id: str) -> bool:
    """POST /join/<roomId> — accept a pending invite; True on HTTP 200 + room_id."""
    code, data = http_json_tolerant(
        "POST", f"{MATRIX_HOMESERVER}/_matrix/client/v3/join/{urllib.parse.quote(room_id, safe='')}",
        bearer=token, body={}, timeout=15)
    if code == 200 and isinstance(data, dict) and data.get("room_id"):
        return True
    err = data.get("errcode") if isinstance(data, dict) else None
    print(f"ai-room-bot: join {room_id} failed (HTTP {code} {err})", flush=True)
    return False


JOIN_HELLO = ("🤖 KI-Bot ist jetzt auch in diesem Raum: !ai <frage>, "
              "!entwurf <empfänger>: <stichpunkte>, !brief")


def handle_invite(token: str, own_user_id: str, room_id: str, served: set[str]) -> None:
    """Invite for the bot account → join, serve the room, announce once."""
    if room_id in served:
        return
    if not join_room(token, room_id):
        return                      # keep serving the rooms we already have
    served.add(room_id)
    print(f"ai-room-bot: joined invited room {room_id} — now serving", flush=True)
    send_text(token, room_id, JOIN_HELLO)


def process_sync(token: str, own_user_id: str, data: dict, served: set[str],
                 catchup: bool = False) -> None:
    """Handle one sync response: invites for own account + served-room messages.

    catchup=True (the timeout=0 boot snapshot) processes invites ONLY — its
    timeline carries the last historical message of each room, and answering
    that on every restart would replay stale conversations.
    """
    rooms = data.get("rooms") or {}
    for iroom, invite_block in (rooms.get("invite") or {}).items():
        events = ((invite_block.get("invite_state") or {}).get("events")) or []
        for ev in events:
            if (ev.get("type") == "m.room.member"
                    and (ev.get("content") or {}).get("membership") == "invite"
                    and ev.get("state_key") == own_user_id):
                handle_invite(token, own_user_id, iroom, served)
                break
    if catchup:
        return
    for jroom, room in (rooms.get("join") or {}).items():
        if jroom not in served:
            continue
        for ev in (room.get("timeline") or {}).get("events") or []:
            handle_event(token, own_user_id, jroom, ev)


# ── LiteLLM gateway ─────────────────────────────────────────────────────────

class GatewayError(Exception):
    """Gateway unusable — the message is a user-facing German hint."""


def gateway_completion(system: str, question: str) -> str:
    """One gateway call; raises GatewayError with an in-room German hint."""
    if not LITELLM_API_KEY:
        raise GatewayError("KI-Service ist nicht konfiguriert (LITELLM_API_KEY fehlt).")
    try:
        _, data = http_json("POST", f"{LITELLM_BASE}/chat/completions", bearer=LITELLM_API_KEY, body={
            "model": AI_BOT_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
        }, timeout=GATEWAY_TIMEOUT)
        choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
        answer = (choice.get("message") or {}).get("content", "")
        answer = answer.strip()
        if not answer:
            raise GatewayError("KI-Service hat ohne Antwort geantwortet.")
        return answer
    except GatewayError:
        raise
    except Exception:
        raise GatewayError("KI-Service ist gerade nicht erreichbar — später nochmal versuchen.") from None


def gateway_chat(question: str, system: str = SYSTEM_PROMPT) -> str:
    """gateway_completion, but every failure becomes a plain German answer."""
    try:
        return gateway_completion(system, question)
    except GatewayError as exc:
        return str(exc)


# ── IMAP handoff (stdlib imaplib only) ──────────────────────────────────────

class ImapError(Exception):
    """IMAP unusable — the message is a user-facing German hint."""


_IMAP_NO_CREDS = (
    "📬 Kein IMAP-Zugang konfiguriert: IMAP_PASSWORD (Stalwart-App-Passwort) "
    "fehlt — einmalig mit scripts/stalwart-app-password.sh anlegen und der "
    "Umgebung des ai-bot Containers hinzufügen."
)
_IMAP_AUTH_FAILED = (
    "📬 IMAP-Login abgelehnt: Stalwart akzeptiert für IMAP nur App-Passwörter "
    "(das normale Konto-Kennwort funktioniert bei PLAIN/LOGIN nicht). "
    "App-Passwort anlegen (scripts/stalwart-app-password.sh) und als "
    "IMAP_PASSWORD setzen."
)


def _ellipsize(text: str, limit: int) -> str:
    text = " ".join(text.split())          # flatten IMAP folded headers
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def imap_connect_and_login() -> imaplib.IMAP4:
    """IMAP4 connect + LOGIN as the bot's mail identity; raises ImapError."""
    try:
        conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
    except OSError as exc:
        raise ImapError(
            f"📬 Postfach gerade nicht erreichbar ({IMAP_HOST}:{IMAP_PORT}) — später nochmal versuchen."
        ) from exc
    password = IMAP_PASSWORD or MATRIX_PASSWORD   # account pw only a fallback:
                                                  # the OIDC directory rejects it
    if not password:
        raise ImapError(_IMAP_NO_CREDS)
    try:
        typ, _ = conn.login(IMAP_USER, password)
    except imaplib.IMAP4.error as exc:
        if "AUTHENTICATIONFAILED" in str(exc).upper():
            raise ImapError(_IMAP_AUTH_FAILED) from exc
        raise ImapError(f"📬 IMAP-Fehler: {_ellipsize(str(exc), 90)}") from exc
    if typ != "OK":
        raise ImapError(_IMAP_AUTH_FAILED)
    return conn


def imap_append_draft(recipient: str, subject: str, body: str) -> int:
    """APPEND a RFC-5322 draft to the Drafts mailbox with \\Draft; → size."""
    msg = EmailMessage()
    msg["From"] = IMAP_USER
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg_id_domain = IMAP_USER.rsplit("@", 1)[-1] or IMAP_MAIL_DOMAIN
    msg["Message-ID"] = email.utils.make_msgid(domain=msg_id_domain)
    msg.set_content(body)
    raw = msg.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")  # CRLF per RFC 3501
    mailbox = f'"{IMAP_DRAFTS}"'
    conn = imap_connect_and_login()
    try:
        for attempt in (0, 1):
            try:
                typ, dat = conn.append(mailbox, r"(\Draft)", None, raw)
            except imaplib.IMAP4.error as exc:
                if attempt == 0 and "TRYCREATE" in str(exc).upper():
                    conn.create(mailbox)       # first draft ever — mailbox missing
                    continue
                raise ImapError(f"📬 Entwurf konnte nicht gespeichert werden ({_ellipsize(str(exc), 90)}.") from exc
            if typ == "OK":
                return len(raw)
            text = " ".join(str(part) for part in (dat or []))
            if attempt == 0 and "TRYCREATE" in text.upper():
                conn.create(mailbox)
                continue
            raise ImapError(f"📬 Entwurf konnte nicht gespeichert werden ({_ellipsize(text, 90)}.")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    raise ImapError("📬 Entwurf konnte nicht gespeichert werden (unbekannter IMAP-Zustand).")


def _short_addr(header_value: object) -> str:
    """'Display Name <a@b>' → 'Display Name' (or the bare addr-spec)."""
    try:
        addresses = header_value.addresses      # email.policy.default header
        if addresses:
            first = addresses[0]
            return first.display_name or first.addr_spec
    except AttributeError:
        pass
    return str(header_value or "")


def _short_date(header_value: object) -> str:
    try:
        parsed = email.utils.parsedate_to_datetime(str(header_value))
        return parsed.strftime("%d.%m. %H:%M")
    except Exception:
        return "—"


def imap_inbox_brief(limit: int) -> str:
    """Unread count + newest items (BODY.PEEK only — flags untouched)."""
    conn = imap_connect_and_login()
    try:
        typ, sel = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise ImapError("📬 INBOX konnte nicht geöffnet werden.")
        total = int(sel[0] or b"0")
        typ, unseen_ids = conn.search(None, "UNSEEN")
        unseen = len((unseen_ids[0] or b"").split()) if typ == "OK" else 0
        typ, all_ids = conn.search(None, "ALL")
        id_list = (all_ids[0] or b"").split() if typ == "OK" else []
        items: list[tuple[str, str, str]] = []
        for msg_id in reversed(id_list[-max(1, limit):]):
            typ, data = conn.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK":
                continue
            for part in data or []:
                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                    headers = email.message_from_bytes(bytes(part[1]), policy=email.policy.default)
                    items.append((
                        _short_date(headers.get("Date")),
                        _ellipsize(_short_addr(headers.get("From")) or "?", 28),
                        _ellipsize(str(headers.get("Subject") or "(ohne Betreff)"), 64),
                    ))
        head = f"📬 {IMAP_USER}: {total} Nachrichten, {unseen} ungelesen"
        if not items:
            return head + " — kein Inhalt." if total == 0 else head + "\n(Header konnten nicht gelesen werden.)"
        lines = "\n".join(f"{i}. {d} — {f}: {s}" for i, (d, f, s) in enumerate(items, 1))
        more = f"\n(+{max(0, total - limit)} ältere nicht angezeigt)" if total > limit else ""
        return f"{head}\nNeueste zuerst:\n{lines}{more}"
    finally:
        try:
            conn.logout()
        except Exception:
            pass


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
    print(f"imap:       host={IMAP_HOST}:{IMAP_PORT} user={IMAP_USER} drafts={IMAP_DRAFTS} "
          f"password={'set' if (IMAP_PASSWORD or MATRIX_PASSWORD) else 'MISSING'}")
    try:
        conn = imap_connect_and_login()
        try:
            typ, sel = conn.select("INBOX", readonly=True)
            if typ == "OK":
                print(f"imap:       OK (INBOX open, {int(sel[0] or b'0')} messages)")
            else:
                problems.append("imap: INBOX select failed")
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except ImapError as exc:
        problems.append(f"imap: {_ellipsize(str(exc), 110)}")
    except Exception as exc:
        problems.append(f"imap unreachable: {exc}")
    if problems:
        for p in problems:
            print(f"problem:    {p}")
        return 1
    print("check:      OK")
    return 0


def cmd_serve() -> int:
    token, user_id = matrix_login()
    room_id = resolve_room(token)
    served: set[str] = {room_id}          # grows with every accepted invite
    print(f"ai-room-bot: serving as {user_id} in {room_id} (model {AI_BOT_MODEL} @ {LITELLM_BASE})", flush=True)

    _, initial = http_json("GET", f"{MATRIX_HOMESERVER}/_matrix/client/v3/sync"
                           f"?timeout=0&filter={{\"room\":{{\"timeline\":{{\"limit\":1}}}}}}", bearer=token, timeout=60)
    since = initial.get("next_batch")
    process_sync(token, user_id, initial, served, catchup=True)   # invites pending at boot
    print("ai-room-bot: caught up with sync, listening for '!ai <question>', '!entwurf …', '!brief'", flush=True)

    while True:
        try:
            _, data = http_json("GET", f"{MATRIX_HOMESERVER}/_matrix/client/v3/sync"
                                f"?timeout={SYNC_POLL_MS}&since={urllib.parse.quote(since, safe='')}",
                                bearer=token, timeout=SYNC_POLL_MS // 1000 + 30)
            since = data.get("next_batch", since)
            process_sync(token, user_id, data, served)
        except KeyboardInterrupt:
            print("ai-room-bot: bye")
            return 0
        except Exception as exc:  # transient network/gateway hiccups — keep serving
            print(f"ai-room-bot: loop error ({exc!r}) — retrying", flush=True)
            time.sleep(3)


# ── room commands: !entwurf / !brief ────────────────────────────────────────

ENTWURF_USAGE = (
    "Benutzung: !entwurf <empfänger>: <stichpunkte>\n"
    "Beispiel: !entwurf kunde@example.com: Dank für das Gespräch, "
    "Angebot bis Freitag zusagen, Rückfrage zum Budget"
)


def split_draft_answer(answer: str) -> tuple[str, str]:
    """>LLM output → (subject, body); expects a leading 'Betreff:' line."""
    lines = answer.strip().splitlines()
    for i, line in enumerate(lines):
        match = re.match(r"^(?:betreff|subject)\s*[:：]\s*(.*)$", line.strip(), re.IGNORECASE)
        if match:
            subject = match.group(1).strip() or "Entwurf"
            body = "\n".join(lines[i + 1:]).strip()
            return subject, body or answer.strip()
    return "Entwurf", answer.strip()


def cmd_entwurf(args: str) -> str:
    """'!entwurf <empfänger>: <stichpunkte>' → draft in SOGo unter „Entwürfe"."""
    if ":" not in args:
        return ENTWURF_USAGE
    recipient, points = (part.strip() for part in args.split(":", 1))
    if not recipient or not points:
        return ENTWURF_USAGE
    try:
        answer = gateway_completion(DRAFT_SYSTEM_PROMPT,
                                    f"Empfänger: {recipient}\nStichpunkte:\n{points}")
    except GatewayError as exc:
        return str(exc)
    subject, body = split_draft_answer(answer)
    try:
        imap_append_draft(recipient, subject, body)
    except ImapError as exc:
        return str(exc)
    return (f"✉️ Entwurf liegt in SOGo unter „Entwürfe“ — An: {recipient} · "
            f"Betreff: {subject}. Bitte dort prüfen und senden.")


def cmd_brief() -> str:
    """'!brief' → unread count + newest inbox items, posted back."""
    try:
        return imap_inbox_brief(AI_BRIEF_LIMIT)
    except ImapError as exc:
        return str(exc)


def handle_command(body: str) -> str:
    """Dispatch '!entwurf'/'!brief' bodies (already own/booking filtered)."""
    if body.startswith(ENTWURF_CMD):
        rest = body[len(ENTWURF_CMD):].strip()
        return cmd_entwurf(rest) if rest else ENTWURF_USAGE
    if body.startswith(BRIEF_CMD):
        rest = body[len(BRIEF_CMD):]
        if not rest.strip():
            return cmd_brief()
    return ("Unbekannter Befehl: " + body.split()[0]
            + " — unterstützt werden !ai, !entwurf, !brief.")


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
    if body.startswith(ENTWURF_CMD) or body.startswith(BRIEF_CMD):
        send_text(token, room_id, handle_command(body))
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
    parser.add_argument("--serve", action="store_true",
                        help="long-poll sync and answer '!ai', '!entwurf <empfänger>: <stichpunkte>', '!brief'")
    parser.add_argument("--ask", metavar="QUESTION", help="one-shot gateway answer to stdout")
    parser.add_argument("--check", action="store_true",
                        help="audit Matrix login, room resolution, gateway reach, IMAP access")
    args = parser.parse_args()
    if args.ask:
        return cmd_ask(args.ask)
    if args.check:
        return cmd_check()
    return cmd_serve()          # default: serve


if __name__ == "__main__":
    sys.exit(main())
