"""openSME VC room app — minimal LiveKit front door.

Responsibilities (and nothing else):
  * `/`            lobby: pick/join a room by name
  * `/room/<name>` room page: server-mints a LiveKit access token for the
                   caller and injects it into the vendored livekit-client UI
  * `/healthz`     compose healthcheck target
  * `/static/*`    vendored livekit-client SDK (no CDN at runtime)

Design constraints:
  * STATELESS — no database, no disk writes. Rooms are ephemeral: they
    exist while participants are connected (LiveKit auto-creates on first
    join) and vanish afterwards. Nothing to back up, nothing that leaks.
  * The API secret never leaves the container — minting is offline HMAC.
  * Room names and display names are strictly validated/length-bounded.
  * Anyone with the link can join any room (same openness as the public
    booking page). Openness is a product choice for the demo; tighten by
    gating this app behind the portal session if it ever matters.
"""

import os
import re
import time
import uuid

import jwt
from flask import Flask, abort, render_template, request

app = Flask(__name__)

API_KEY = os.environ["LIVEKIT_API_KEY"]
API_SECRET = os.environ["LIVEKIT_API_SECRET"]
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "wss://localhost/rtc")
VC_TITLE = os.environ.get("VC_TITLE", "openSME Meet")
TOKEN_TTL_S = int(os.environ.get("VC_TOKEN_TTL_S", str(12 * 3600)))

ROOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
NAME_RE = re.compile(r"^[^<>\"'&]{1,40}$")


def mint_token(room: str, identity: str, display: str) -> str:
    now = int(time.time())
    claims = {
        "iss": API_KEY,
        "sub": identity,
        "iat": now,
        "exp": now + TOKEN_TTL_S,
        "name": display,
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    return jwt.encode(claims, API_SECRET, algorithm="HS256")


def safe_display(raw: str) -> str:
    display = (raw or "").strip()
    if not display:
        display = "guest"
    if not NAME_RE.match(display):
        abort(400, description="invalid display name")
    return display


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def lobby():
    return render_template("lobby.html", title=VC_TITLE)


@app.get("/room/<name>")
def room(name: str):
    if not ROOM_RE.match(name):
        abort(404)
    display = safe_display(request.args.get("name", ""))
    identity = f"{display[:24]}-{uuid.uuid4().hex[:8]}"
    token = mint_token(name, identity, display)
    return render_template(
        "room.html",
        title=VC_TITLE,
        room=name,
        display=display,
        token=token,
        server_url=LIVEKIT_URL,
    )
