#!/usr/bin/env bash
# provision-sogo-caldav.sh — connect testuser's SOGo6 calendar to calrs.
#
# What it provisions (idempotent, safe to re-run):
#   1. the sogo6 agent worker (python -m app.agent.run) inside
#      opensme-sogo6-server — without it, external-calendar sync jobs stay
#      "pending" forever (the image's CMD only starts gunicorn)
#   2. an external CalDAV calendar subscription on the testuser's sogo6
#      account pointing at the calrs booking store exposed at the edge:
#        https://<CALENDAR_DOMAIN>/dav/booking/main/   (Radicale behind calrs,
#        Traefik stripprefix /dav, Radicale's own htpasswd auth on top)
#      Credentials: RADICALE_USER / RADICALE_PASSWORD (the booking store
#      login — the store holds ONLY public bookings, never staff calendars).
#
#      NOTE the collection layout: /booking/ in Radicale is the principal
#      calendar-home-set (a BARE collection); actual calendars live as
#      children (scripts/calendar-setup.sh MKCALENDARs /booking/main/).
#      The subscription must target the calendar COLLECTION href
#      /dav/booking/main/, not the home — REPORT on the home returns an
#      empty multistatus and the mirror sees nothing.
#
# Why the edge URL: sogo6's IcsFetcher is https-only and rejects private
# IPs (SSRF guard), so the internal http://calendar-radicale:5232/ name is
# not usable from a subscribing client; the public hostname resolves to the
# VPS IP and is reached via NAT hairpin (calendar-radicale is pinned to
# 172.19.0.250 in docker-compose.yml for internal clients that accept IP
# literals).
#
# Known limitation: none — sogo6 (since 1e60879) fetches CalDAV calendars
# via REPORT calendar-query on the collection URL (Radicale answers GET on a
# collection with 403), so the mirror sync completes against /dav/booking/main/.
# The CalDAV connection is verified here via PROPFIND (207) through the exact
# URL + credentials the subscription stores.
#
# Env (all have live defaults from /opt/opensme-compose/.env on the VPS):
#   WEBMAIL_URL / SOGO6_URL   default https://webmail.home.opensme.graphwiz.ai
#   CALENDAR_URL              default https://calendar.home.opensme.graphwiz.ai
#   E2E_ZITADEL_PAT, E2E_ZITADEL_LOGINCLIENT_PAT   (machinekey PATs)
#   E2E_TESTUSER_PASSWORD     default eval2026
#   RADICALE_USER, RADICALE_PASSWORD
set -euo pipefail

VPS="${VPS:?set VPS=user@host (SSH target of the deployment)}"
WM="${WEBMAIL_URL:-${SOGO6_URL:-https://webmail.home.opensme.graphwiz.ai}}"
CAL="${CALENDAR_URL:-https://calendar.home.opensme.graphwiz.ai}"
CALDAV_URL="${CALDAV_URL:-$CAL/dav/booking/main/}"
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
RAD_USER = "$RAD_USER"
RAD_PW = "$RAD_PW"

# The API serializers NEVER echo sync_config (credentials are create-only),
# so URL/creds can only be repaired at the DB row. Read the source of truth
# directly (this script runs ON the VPS; sogo6 stores it in Postgres).
import subprocess

def psql(sql: str) -> str:
    out = subprocess.run(
        ["docker", "exec", "opensme-sogo6-postgres", "psql", "-U", "sogo", "-d", "sogo",
         "-tAc", sql],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        print("PSQL ERROR:", (out.stderr or "").strip()[:300]); sys.exit(1)
    return out.stdout.strip()

rows = psql(
    "SELECT key, sync_config::text FROM sogo6_calendar_calendars "
    f"WHERE user_uid='$TESTUSER_MAIL' AND source_type='caldav' ORDER BY created_at")

key = None
if rows:
    first = rows.splitlines()[0]
    key, cfg_json = first.split("|", 1)
    cfg = json.loads(cfg_json)
    need = False
    if cfg.get("url") != CALDAV_URL:
        print(f"→ subscription url {cfg.get('url')!r} != {CALDAV_URL!r}")
        need = True
    if cfg.get("username") != RAD_USER or cfg.get("password") != RAD_PW:
        print("→ subscription credentials stale/absent (API update drops them)")
        need = True
    if need:
        # preserve the sync-status bookkeeping fields; only fix url+creds+interval
        new_cfg = {k: v for k, v in cfg.items()
                   if k not in ("url", "username", "password", "sync_interval_minutes")}
        new_cfg.update({"url": CALDAV_URL, "username": RAD_USER, "password": RAD_PW,
                        "sync_interval_minutes": cfg.get("sync_interval_minutes", 30)})
        js = json.dumps(new_cfg)
        psql(
            "UPDATE sogo6_calendar_calendars SET sync_config = '" + js.replace("'", "''") +
            "'::jsonb, updated_at = now() " +
            f"WHERE key='{key}'")
        print("→ subscription DB row repaired (url + creds):", key)
    else:
        print("→ subscription already present and correct:", key)
else:
    # No caldav subscription yet — create it via the API (creds are
    # create-only; carry them here).
    rr = s.post(f"{WM}/api/user/v1/external-calendars", headers=h, timeout=T, json={
        "name": "calrs (Public Calendar)", "url": CALDAV_URL,
        "source_type": "caldav", "color": "#7C3AED",
        "username": RAD_USER, "password": RAD_PW,
        "sync_interval_minutes": 30})
    key = ((rr.json().get("data") or {}).get("key")
           or ((rr.json().get("data") or {}).get("id") or ""))
    if key:
        print("→ subscription created:", key, "(POST", rr.status_code, ")")
    else:
        print("CREATE FAILED:", rr.status_code, rr.text[:200]); sys.exit(1)

# dedupe: any caldav row beyond the first with this url gets removed
allcal = psql(
    "SELECT key, sync_config->>'url' FROM sogo6_calendar_calendars "
    f"WHERE user_uid='$TESTUSER_MAIL' AND source_type='caldav' ORDER BY created_at")
lines = [ln for ln in allcal.splitlines() if ln]
for ln in lines[1:]:
    dup_key, dup_url = ln.split("|", 1)
    if key and dup_key != key and dup_url == CALDAV_URL:
        s.delete(f"{WM}/api/user/v1/external-calendars/{dup_key}", headers=h, timeout=T)
        print("→ duplicate subscription removed:", dup_key)

# cleanup: drop leftover plain-ICS subscriptions under the testuser (e.g. the
# historical "proxy-test" pointing at example.com/test.ics from proxy-502
# debugging). The booking store is CalDAV-only; a leftover ICS row would show
# up as a broken calendar in the sogo6 UI. This keeps the account exactly in
# the "connected + preconfigured" state: personal (local) + calrs (caldav).
ics_rows = psql(
    "SELECT key FROM sogo6_calendar_calendars "
    f"WHERE user_uid='$TESTUSER_MAIL' AND source_type='ics' ORDER BY created_at")
for ics_key in [ln for ln in ics_rows.splitlines() if ln]:
    s.delete(f"{WM}/api/user/v1/external-calendars/{ics_key}", headers=h, timeout=T)
    print("→ leftover ics subscription removed:", ics_key)

if not key:
    print("NO KEY — aborting"); sys.exit(1)
s.post(f"{WM}/api/user/v1/external-calendars/{key}/sync", headers=h, timeout=T)
print("→ sync triggered against", CALDAV_URL)
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
