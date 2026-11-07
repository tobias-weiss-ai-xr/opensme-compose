# Minimal LiveKit token mint + room UI for openSME VC.
#
# Token minting is pure HMAC-SHA256 (pyjwt) — no LiveKit API calls at
# runtime, so the app does not need to reach the SFU over the network;
# it only shares LIVEKIT_API_KEY/SECRET with it via the compose env.
#
# The livekit-client SDK is vendored at BUILD time from the pinned
# version below (browsers get it from this app's /static, no CDN —
# consistent with the stack's no-runtime-egress posture).
FROM python:3.12-slim

# strip setuid/setgid bits: the app runs unprivileged with cap_drop: ALL
# (static suite flags setuid binaries in an image that drops ALL caps)
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# The SDK is stored under a FIXED name (livekit-client.umd.min.js) so the
# room template stays stable regardless of which dist filename unpkg serves
# (livekit-client@2.x currently ships only the unminified .umd.js).
ARG LIVEKIT_CLIENT_VERSION=2.23.0
RUN pip install --no-cache-dir "flask>=3.0" "pyjwt>=2.8" \
 && mkdir -p /app/static \
 && python - "$LIVEKIT_CLIENT_VERSION" <<'PY'
import sys, urllib.request, pathlib
v = sys.argv[1]
dest = pathlib.Path("/app/static/livekit-client.umd.min.js")
for name in ("livekit-client.umd.min.js", "livekit-client.umd.js"):
    url = f"https://unpkg.com/livekit-client@{v}/dist/{name}"
    try:
        data = urllib.request.urlopen(url, timeout=60).read()
        assert len(data) > 10_000, f"suspiciously small: {len(data)}"
        dest.write_bytes(data)
        print(f"vendored {name} -> {dest.name} ({len(data)} bytes)")
        break
    except Exception as e:
        last = e
else:
    raise SystemExit(f"could not vendor livekit-client SDK: {last}")
PY

WORKDIR /app
COPY app.py /app/app.py
COPY templates /app/templates
ENV FLASK_APP=/app/app.py
EXPOSE 8090
# read-only root friendly: Flask writes nothing by default
CMD ["python", "-c", "from app import app; app.run(host='0.0.0.0', port=8090)"]
