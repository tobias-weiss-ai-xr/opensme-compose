#!/usr/bin/env bash
# provision-sogo-caldav.sh — connect testuser's SOGo6 calendar to calrs.
#
# What it provisions (idempotent, safe to re-run):
#   1. the sogo6 agent worker (python -m app.agent.run) inside
#      opensme-sogo6-server — without it, external-calendar sync jobs stay
#      "pending" forever (the image's CMD only starts gunicorn)
#   2. an external CalDAV calendar subscription on the testuser's sogo6
#      account pointing at the calrs booking store exposed at the edge:
#        https://<CALENDAR_DOMAIN>/dav/booking/   (Radicale behind calrs,
#        Traefik stripprefix /dav, Radicale's own htpasswd auth on top)
#      Credentials: RADICALE_USER / RADICALE_PASSWORD (the booking store
#      login — the store holds ONLY public bookings, never staff calendars).
#
# Why the edge URL: sogo6's IcsFetcher is https-only and rejects private
# IPs (SSRF guard), so the internal http://calendar-radicale:5232/ name is
# not usable from a subscribing client; the public hostname resolves to the
# VPS IP and is reached via NAT hairpin (calendar-radicale is pinned to
# 172.19.0.250 in docker-compose.yml for internal clients that accept IP
# literals).
#
# Known limitation: sogo6's sync engine GETs the subscription URL first and
# Radicale answers GET on a collection with 403, so the automatic ICS mirror
# reports "Failed To Fetch ICS Calendar Feed" until sogo6 grows a REPORT-
# based fetch. The CalDAV connection itself is verified here via PROPFIND
# (207) through the exact URL + credentials the subscription stores.
#
# Env (all have live defaults from /opt/opensme-compose/.env on the VPS):
#   WEBMAIL_URL / SOGO6_URL   default https://webmail.home.opensme.graphwiz.ai
#   CALENDAR_URL              default https://calendar.home.opensme.graphwiz.ai
#   E2E_ZITADEL_PAT, E2E_ZITADEL_LOGINCLIENT_PAT   (machinekey PATs)
#   E2E_TESTUSER_PASSWORD     default eval2026
#   RADICALE_USER, RADICALE_PASSWORD
set -euo pipefail

VPS="${VPS:-root@195.90.216.140}"
WM="${WEBMAIL_URL:-${SOGO6_URL:-https://webmail.home.opensme.graphwiz.ai}}"
CAL="${CALENDAR_URL:-https://calendar.home.opensme.graphwiz.ai}"
CALDAV_URL="${CALDAV_URL:-$CAL/dav/booking/}"
TESTUSER_MAIL="${TESTUSER_MAIL:-testuser@opensme.local}"
TESTUSER_PW="${E2E_TESTUSER_PASSWORD:-eval2026}"

fetch() { ssh "$VPS" "$1"; }

PAT=$(fetch 'docker cp opensme-zitadel:/machinekey/pat - | tar -xO' | tr -d '\n\r')
LCPAT=$(fetch 'docker cp opensme-zitadel:/machinekey/login-client.pat - | tar -xO' | tr -d '\n\r')
RAD_USER=$(fetch 'grep "^RADICALE_USER=" /opt/opensme-compose/.env | cut -d= -f2-')
RAD_PW=$(fetch 'grep "^RADICALE_PASSWORD=" /opt/opensme-compose/.env | cut -d= -f2-')

# 1. agent worker (idempotent)
if ! fetch 'for p in $(ls /proc | grep -E "^[0-9]+$"); do tr "\0" " " < /proc/$p/cmdline 2>/dev/null; done | grep -q "app.agent.run"'; then
  echo "→ starting sogo6 agent worker"
  fetch 'docker exec -d -w /app opensme-sogo6-server /usr/local/bin/python3.14 -m app.agent.run'
else
  echo "→ sogo6 agent worker already running"
fi

# 2. subscription via the sogo6 API as testuser (OIDC login through Zitadel)
echo "→ provisioning external CalDAV subscription on $TESTUSER_MAIL"
TMPPY=$(mktemp)
cat > "$TMPPY" <<PYEOF
import sys, os, json, time, urllib.parse
sys.path.insert(0, "/opt/opensme-compose/compose/tests/05-e2e")
import requests, urllib3
urllib3.disable_warnings()
from run import T, api_login, resolve_user_id
WM = "$WM"; IDP = "https://auth.home.opensme.graphwiz.ai"
lc = "$LCPAT"; pat = "$PAT"
uid = resolve_user_id(requests.Session(), IDP, pat, "testuser")
s = requests.Session(); s.verify = False
r = s.get(f"{WM}/api/user/v1/auth/mode",
          params={"username": "$TESTUSER_MAIL", "redirect": WM + "/"}, timeout=T)
authz = r.json()["data"]["location"]
qs, _ = api_login(s, IDP, lc, uid, authz, uid, "$TESTUSER_PW")
ru = urllib.parse.parse_qs(urllib.parse.urlparse(authz).query)["redirect_uri"][0]
r = s.get(ru + "?" + urllib.parse.urlencode(qs, doseq=True), timeout=T, allow_redirects=True)
h = {"Authorization": "Bearer " + r.url.split("#token=")[-1]}
CALDAV_URL = "$CALDAV_URL"
# NOTE: /external-calendars lists only source_type=ics; /calendars lists
# all — match the caldav subscription there
r = s.get(f"{WM}/api/user/v1/calendars", headers=h, timeout=T)
cals = (r.json().get("data") or {}).get("calendars", [])
mine = [c for c in cals if c.get("source_type") == "caldav"
        and (c.get("sync_config") or {}).get("url") == CALDAV_URL]
key = None
r = s.get(f"{WM}/api/user/v1/calendars", headers=h, timeout=T)
allcal = [c for c in ((r.json().get("data") or {}).get("calendars", []))
          if c.get("source_type") == "caldav"]
if mine:
    key = mine[0]["key"]; print("→ subscription already present:", key)
elif allcal:
    # stale URL (e.g. pre-edge IP literal) — repoint the first, delete dupes
    key = allcal[0]["key"]
    r = s.put(f"{WM}/api/user/v1/external-calendars/{key}", headers=h, timeout=T,
              json={"sync_config": {"url": CALDAV_URL}})
    print("→ subscription repointed to edge URL:", key, "(PUT", r.status_code, ")")
    for dup in allcal[1:]:
        s.delete(f"{WM}/api/user/v1/external-calendars/{dup['key']}", headers=h, timeout=T)
        print("→ duplicate subscription removed:", dup["key"])
else:
    body = {"name": "calrs (Public Calendar)", "url": CALDAV_URL,
            "source_type": "caldav", "color": "#7C3AED",
            "username": "$RAD_USER", "password": "$RAD_PW",
            "sync_interval_minutes": 30}
    # create persists before the proxy surfaces a post-create fetch error
    # (502) — re-list to learn the key; 422 means validation rejected the
    # body, which is a real failure
    r2 = s.get(f"{WM}/api/user/v1/calendars", headers=h, timeout=T)
    mine = [c for c in ((r2.json().get("data") or {}).get("calendars", []))
            if c.get("source_type") == "caldav"
            and (c.get("sync_config") or {}).get("url") == CALDAV_URL]
    if mine:
        key = mine[0]["key"]; print("→ subscription created:", key)
    else:
        print("CREATE FAILED:", r.status_code, r.text[:200]); sys.exit(1)
s.post(f"{WM}/api/user/v1/external-calendars/{key}/sync", headers=h, timeout=T)
print("→ sync triggered (job runs in the agent worker)")
PYEOF
scp -q "$TMPPY" "$VPS":/tmp/sogo_caldav_provision.py
rm -f "$TMPPY"
ssh "$VPS" 'python3 /tmp/sogo_caldav_provision.py'

# 3. verify the CalDAV connection itself (PROPFIND through the edge URL)
echo "→ verifying CalDAV endpoint $CALDAV_URL"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" -u "$RAD_USER:$RAD_PW" \
  -X PROPFIND -H "Depth: 0" "$CALDAV_URL")
ANON=$(curl -sk -o /dev/null -w "%{http_code}" -X PROPFIND -H "Depth: 0" "$CALDAV_URL")
echo "  PROPFIND with booking auth: $CODE (want 207)"
echo "  PROPFIND anonymous:         $ANON (want 401)"
[ "$CODE" = "207" ] && [ "$ANON" = "401" ] && echo "OK: testuser SOGo CalDAV ↔ calrs connected" \
  || { echo "FAIL: CalDAV endpoint check"; exit 1; }
