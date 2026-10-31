#!/usr/bin/env bash
# stalwart-app-password.sh — seed a Stalwart app password for an openSME user.
#
# WHY: Stalwart's OIDC directory accepts PLAIN/LOGIN only for *app passwords*
#      (webmail itself authenticates via XOAUTH2 with the OIDC access token).
#      Regular IMAP/SMTP clients (Thunderbird, phone mail apps) need an app
#      password. Stalwart >=0.16 manages app passwords over JMAP (x:AppPassword),
#      but **Stalwart 0.15.x exposes them via the REST endpoint
#      POST /api/account/auth** (self-service, ops array) — which is what this
#      script drives.
#
# The script re-uses the user's live OIDC access token that SOGo6 stores in
# Redis after an SSO login (key: user_oidc_session:<email>), so no extra
# credentials are needed.
#
# Usage:
#   sudo ./stalwart-app-password.sh <user-email> [label]
#
# Example:
#   sudo ./stalwart-app-password.sh testuer@opensme.auth.home.opensme.graphwiz.ai
#
# Output: the plaintext secret on stdout (only printed once — save it for the
#         client you configure).
#
set -euo pipefail

EMAIL="${1:?usage: stalwart-app-password.sh <user-email> [label]}"
LABEL="${2:-sogo-webmail}"
REDIS_C="opensme-sogo6-redis"
SERVER_C="opensme-sogo6-server"
STALWART_PORT="${STALWART_PORT:-8080}"

# --- 1. obtain the OIDC access token from the SOGo6 Redis session ------------
if [[ -n "${STALWART_TOKEN_FILE:-}" && -f "$STALWART_TOKEN_FILE" ]]; then
    TOKEN="$(cat "$STALWART_TOKEN_FILE")"
    echo "[info] using access token from $STALWART_TOKEN_FILE" >&2
else
    KEY="user_oidc_session:${EMAIL}"
    RAW="$(docker exec "$REDIS_C" redis-cli GET "$KEY" || true)"
    if [[ -z "$RAW" || "$RAW" == "(nil)" ]]; then
        echo "[info] ERROR: no OIDC session in redis for $EMAIL (key $KEY)." >&2
        echo "[info] The user must have logged into webmail at least once (SSO stores the token)," >&2
        echo "[info] or pass STALWART_TOKEN_FILE=<path> with an access token." >&2
        exit 1
    fi
    # access_token extraction (redis value is a JSON document)
    if command -v jq >/dev/null 2>&1; then
        TOKEN="$(printf '%s' "$RAW" | jq -r '.access_token')"
    else
        TOKEN="$(printf '%s' "$RAW" | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')"
    fi
    [[ -n "${TOKEN:-}" ]] || { echo "[info] ERROR: no access_token field in redis payload." >&2; exit 1; }
    echo "[info] using SSO access token from redis session of $EMAIL" >&2
fi

# --- 2. client-side secret + sha512-crypt hash + base64 name ------------------
SECRET="app_$(openssl rand -base64 12 | tr '+/' '-_' | tr -d '=')"
SALT="$(openssl rand -base64 9 | tr '+.' './' | tr -d '=' | cut -c1-16)"
HASH="$(openssl passwd -6 -salt "$SALT" "$SECRET")"
NAME="$(printf '%s$%s' "$LABEL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | base64 -w0)"

# --- 3. create via Stalwart 0.15 REST (self-service bearer) ------------------
cat > /tmp/stalwart_app_pw_post.py <<'PY'
import json, os, sys, urllib.request
HOST = "http://stalwart:%s" % os.environ["SP_PORT"]
at, name, hashed = os.environ["SP_TOKEN"], os.environ["SP_NAME"], os.environ["SP_HASH"]
req = urllib.request.Request(
    HOST + "/api/account/auth", method="POST",
    data=json.dumps([{"type": "addAppPassword", "name": name, "password": hashed}]).encode(),
    headers={"Content-Type": "application/json", "Accept": "application/json",
             "Authorization": "Bearer " + at})
try:
    r = urllib.request.urlopen(req, timeout=15)
    print("POST /api/account/auth ->", r.status)
except urllib.error.HTTPError as e:
    print("POST FAILED:", e.code, e.read(300).decode(errors="replace"))
    sys.exit(2)
PY
docker cp /tmp/stalwart_app_pw_post.py "$SERVER_C":/tmp/stalwart_app_pw_post.py >/dev/null
docker exec -e SP_PORT="$STALWART_PORT" -e SP_TOKEN="$TOKEN" -e SP_NAME="$NAME" -e SP_HASH="$HASH" \
    "$SERVER_C" python3 /tmp/stalwart_app_pw_post.py

# --- 4. verify PLAIN/LOGIN with the fresh app password via IMAP ----------------
cat > /tmp/stalwart_app_pw_imap.py <<'PY'
import imaplib, os, sys
try:
    m = imaplib.IMAP4("stalwart", 143, timeout=10)
    m.login(os.environ["SP_EMAIL"], os.environ["SP_SECRET"])
    t, d = m.status("INBOX", "(MESSAGES)")
    print("IMAP LOGIN OK:", t, d)
    m.logout()
except Exception as e:
    print("IMAP LOGIN FAIL:", str(e)[:120])
    sys.exit(3)
PY
docker cp /tmp/stalwart_app_pw_imap.py "$SERVER_C":/tmp/stalwart_app_pw_imap.py >/dev/null
docker exec -e SP_EMAIL="$EMAIL" -e SP_SECRET="$SECRET" "$SERVER_C" python3 /tmp/stalwart_app_pw_imap.py

echo ""
echo "======================================================"
echo " APP PASSWORD CREATED for $EMAIL"
echo "   label : $LABEL"
echo "   secret: $SECRET"
echo "   IMAP  : stalwart:143  (login: $EMAIL / <secret>)"
echo "   SMTP  : stalwart:587  (STARTTLS, same credentials)"
echo " NOTE: the secret is only shown once — store it in your client now."
echo "======================================================"
