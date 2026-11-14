#!/usr/bin/env bash
# scripts/booking-notify.sh — Booking-to-Element notification bridge
# ══════════════════════════════════════════════════════════════════════
# Polls the calrs SQLite booking db for confirmed bookings that have not
# been announced yet and posts a Matrix m.room.message into the support
# Element room (Synapse client API), so confirmed calrs bookings show up
# in the chat.home.opensme.graphwiz.ai Element client.
#
# Modes:
#   booking-notify.sh              one poll+notify pass (apply)
#   booking-notify.sh --dry-run    print what WOULD be posted; no login,
#                                  no posts, no state/room mutations; exits 0
#   booking-notify.sh --check      configuration + connectivity audit,
#                                  read-only; exit != 0 on failed checks
#   booking-notify.sh --loop [N]   poll forever, pass every N seconds
#                                  (default 60)
#   --env-file PATH                env file to load (default .env in the
#                                  repo root); missing file is fine — the
#                                  defaults below are complete
#   --help, -h                     this help
#
# Environment (explicit environment beats the env file, docker semantics):
#   MATRIX_DOMAIN       Matrix server_name      (env MATRIX_DOMAIN, default
#                       matrix.home.opensme.graphwiz.ai)
#   MATRIX_HOMESERVER   client API base URL     (default https://$MATRIX_DOMAIN)
#   MATRIX_USER         posting account         (default testuser — the
#                       bootstrap/demo-users.json test user)
#   MATRIX_PASSWORD     its password            (default $E2E_TESTUSER_PASSWORD
#                       or eval2026, the demo baseline — same convention as
#                       scripts/provision-sogo-caldav.sh)
#   MATRIX_ROOM         target room: '!room_id' or '#alias' (default
#                       #support:$MATRIX_DOMAIN; created automatically when
#                       missing — see below)
#   MATRIX_ROOM_INVITE  optional comma-separated mxids invited at creation
#   CALENDAR_CONTAINER  calrs container         (default opensme-calendar)
#   HELPER_IMAGE        sqlite/file helper image (default alpine:3.19)
#
# Secrets (MATRIX_PASSWORD, access tokens) are NEVER printed: the login
# body and every POST body travel via stdin/temp files, the access token
# is passed to curl through an -H @file (0600, removed on exit) so it
# never appears on the process list, and `set +x` forbids tracing.
#
# Verified design notes (live stack, 2026-11-14):
#   * Synapse 1.162.0 answers /_matrix/client/v3/... but NOT the legacy
#     /r3/ prefix (M_UNRECOGNIZED) — this script uses v3 endpoints.
#   * Send uses PUT .../send/m.room.message/<txnId> with the booking id
#     as txnId: the send is idempotent per booking even across retries.
#   * calrs v1.6.0 ships no sqlite3/python3 in its container, so db reads
#     use the calendar-setup.sh step-9 helper pattern (read-only mount of
#     the calrs-data volume into alpine:3.19 + apk add sqlite; WAL readers
#     don't block calrs).
#   * Idempotency: every posted booking id is appended to
#     /var/lib/calrs/booking-notify.state (inside the calrs-data volume,
#     written through the same helper container — a plain file, so no
#     sqlite write locks against the live calrs process). Bookings whose
#     id is in the state file are never announced twice.
#   * No `docker compose` dependency on purpose: compose config
#     interpolation on the deployed tree requires profile vars (e.g.
#     LIVEKIT_API_SECRET) that a bare checkout .env does not carry; the
#     volume is resolved from the running calendar container instead.
#
# Exit codes: 0 success/dry-run, 1 runtime failure, 2 usage error.
# Requires: docker, jq, curl (CONTRIBUTING baseline).
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail
set +x   # never trace: secrets flow through temp files below

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC}  $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1

usage() {
  sed -n '2,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

MODE="apply"          # apply | dry-run | check
LOOP_MODE=false
LOOP_INTERVAL=60
ENV_FILE="$ROOT/.env"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)  MODE="dry-run"; shift ;;
    --check)    MODE="check"; shift ;;
    --loop)     LOOP_MODE=true; MODE="apply"
                if [[ "${2:-}" =~ ^[0-9]+$ ]]; then LOOP_INTERVAL="$2"; shift; fi
                shift ;;
    --env-file) [[ -n "${2:-}" ]] || { err "--env-file requires a value"; exit 2; }
                ENV_FILE="$2"; shift 2 ;;
    --help|-h)  usage; exit 0 ;;
    *)          err "Unknown option: $1 (see --help)"; exit 2 ;;
  esac
done

for tool in docker jq curl; do
  command -v "$tool" >/dev/null 2>&1 || { err "Required tool not found: ${tool}"; exit 1; }
done

# ── env-quote hardening (incident: 'Source: command not found') ────────────
# Same hardened dotenv loader as calendar-setup.sh / demo-data.sh (the
# block between the markers below is byte-identical across the three
# scripts; tests/00-static/check_env_quote.py pins the pattern). Every
# dotenv load scrubs unquoted multi-word values FIRST: offending lines
# are rewritten double-quoted in place (with a .bak-<date> backup and a
# loud warning naming file+line) before the `source`, and the loader can
# never die on such a line. Idempotent: quoted lines never match.
# env-quote-hardening:BEGIN
env_quote_warn() { printf '[env-quote] WARNING: %s\n' "$*" >&2; }
env_quote_scrub() {
  # usage: env_quote_scrub <env-file> [display-label]
  local file="$1" label="${2:-$1}" re bad bak tmp line key val changed=0
  [[ -f "$file" ]] || return 0
  re="^[A-Za-z_][A-Za-z0-9_]*=[^\"'].*[[:space:]]"
  bad="$(grep -nE "$re" "$file" 2>/dev/null || true)"
  [[ -z "$bad" ]] && return 0
  while IFS= read -r line; do
    env_quote_warn "unquoted multi-word env value would tokenize on source: ${label}:${line%%:*}"
  done <<< "$bad"
  bak="${file}.bak-$(date +%Y%m%d-%H%M%S)"
  if ! cp -p "$file" "$bak" 2>/dev/null; then
    env_quote_warn "cannot back up ${label} — leaving the file untouched, load continues"
    return 0
  fi
  tmp="$(mktemp "${file}.scrub.XXXXXX" 2>/dev/null)" || {
    env_quote_warn "no scratch file for ${label} — leaving the file untouched, load continues"
    return 0
  }
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ $re ]]; then
      key="${line%%=*}"
      val="${line#*=}"
      val="${val%$'\r'}"
      val="${val//\"/\\\"}"
      printf '%s="%s"\n' "$key" "$val" >>"$tmp"
      changed=$((changed + 1))
    else
      printf '%s\n' "$line" >>"$tmp"
    fi
  done <"$file"
  if ! mv "$tmp" "$file" 2>/dev/null; then
    env_quote_warn "cannot rewrite ${label} — load continues with the original file"
    rm -f "$tmp"
    return 0
  fi
  env_quote_warn "quoted ${changed} env value(s) in ${label} (backup: ${bak})"
  return 0
}
env_quote_load() {
  # usage: env_quote_load <env-file> [display-label]
  # scrub, then source; NEVER dies on an unquoted multi-word line.
  env_quote_scrub "$1" "${2:-$1}" || true
  # shellcheck disable=SC1090
  source "$1"
}
# env-quote-hardening:END

# explicit environment beats the env file (docker semantics): capture
# the operator's values BEFORE sourcing and re-apply them afterwards
ENV_KEYS=(MATRIX_DOMAIN MATRIX_HOMESERVER MATRIX_USER MATRIX_PASSWORD
          MATRIX_ROOM MATRIX_ROOM_INVITE CALENDAR_CONTAINER HELPER_IMAGE)
declare -A ENV_KEEP=()
for v in "${ENV_KEYS[@]}"; do
  [[ -n "${!v:-}" ]] && ENV_KEEP["$v"]="${!v}"
done
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null  # generated at deploy time
  env_quote_load "$ENV_FILE"
  set +a
else
  warn "env file '${ENV_FILE#$ROOT/}' not found — using documented defaults (pass --env-file to override)"
fi
for v in "${!ENV_KEEP[@]}"; do
  printf -v "$v" '%s' "${ENV_KEEP[$v]}"
done
unset ENV_KEEP

# ── defaults (complete even without any env file) ───────────────────────────
MATRIX_DOMAIN="${MATRIX_DOMAIN:-matrix.home.opensme.graphwiz.ai}"
MATRIX_HOMESERVER="${MATRIX_HOMESERVER:-https://${MATRIX_DOMAIN}}"
MATRIX_USER="${MATRIX_USER:-testuser}"
# demo baseline password — same convention as provision-sogo-caldav.sh
MATRIX_PASSWORD="${MATRIX_PASSWORD:-${E2E_TESTUSER_PASSWORD:-eval2026}}"
MATRIX_ROOM="${MATRIX_ROOM:-#support:${MATRIX_DOMAIN}}"
MATRIX_ROOM_INVITE="${MATRIX_ROOM_INVITE:-}"
CALENDAR_CONTAINER="${CALENDAR_CONTAINER:-opensme-calendar}"
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3.19}"
STATE_FILE="/data/booking-notify.state"   # inside the calrs-data volume
DB_FILE="/data/calrs.db"                  # = /var/lib/calrs/calrs.db in calrs

CALRS_MOUNT="/var/lib/calrs"

# volume_of — resolve the calrs-data docker volume from the RUNNING
# calendar container (robust against compose project-name drift; no
# `docker compose` call, see header). Empty output = unresolved.
volume_of() {
  local vol
  vol="$(docker inspect "$CALENDAR_CONTAINER" --format \
    '{{range .Mounts}}{{if eq .Destination "'"${CALRS_MOUNT}"'"}}{{.Name}}{{end}}{{end}}' \
    2>/dev/null || true)"
  if [[ -z "$vol" ]]; then
    vol="$(docker volume ls --format '{{.Name}}' 2>/dev/null \
      | grep -F 'calrs-data' | head -n1 || true)"
  fi
  printf '%s' "$vol"
}

# helper_sh <rw|ro> <shell-snippet> [stdin] — run a snippet with the
# calrs-data volume mounted at /data (calendar-setup.sh step-9 pattern).
helper_sh() {
  local mode="$1" snippet="$2" ro_flag=":ro"
  if [[ "$mode" == "rw" ]]; then
    ro_flag=""
  fi
  docker run --rm -i -v "${CALRS_VOLUME}:/data${ro_flag}" "$HELPER_IMAGE" sh -c "$snippet"
}

# state_ids — print the recorded booking ids (one per line). The helper
# return code is load-bearing: callers MUST treat a failure as
# "state unknown" and skip the pass (a blind "empty" would re-post).
state_ids() {
  helper_sh ro "cat ${STATE_FILE} 2>/dev/null || true"
}

# state_add <booking-id> — append one id to the state file (atomic-enough
# single-line appends; small writes never interleave on local volumes)
state_add() {
  printf '%s\n' "$1" | helper_sh rw "cat >> ${STATE_FILE}"
}

# db_bookings — confirmed bookings as one JSON object per line
# (sqlite3 JSON1, alpine 3.19 ships 3.44+). Empty output = db UNKNOWN
# (helper/apk failure) — callers treat that as "no data", never as
# "nothing to do" with confidence.
db_bookings() {
  printf '%s\n' \
    "select json_object('id',b.id,'name',b.guest_name,'email',b.guest_email," \
    "'tz',b.guest_timezone,'start',b.start_at,'event',coalesce(e.title,''))" \
    "from bookings b left join event_types e on e.id=b.event_type_id" \
    "where b.status='confirmed' order by b.created_at, b.id;" \
    | helper_sh ro "apk add --no-cache sqlite >/dev/null 2>&1 && sqlite3 ${DB_FILE}" 2>/dev/null \
    || true
}

# ── Matrix client (Synapse client API, /_matrix/client/v3) ────────────────
# Secrets discipline: request/response bodies and the access token move
# through stdin, temp files and shell variables only — never argv, never
# stdout. MX_CODE/MX_BODY carry the last HTTP status/body.

MX_TOKEN=""
MX_CODE="000"
MX_BODY=""
MX_AUTH_FILE=""
TMP_FILES=()
cleanup() {
  local f
  for f in ${TMP_FILES[@]+"${TMP_FILES[@]}"}; do
    [[ -n "$f" ]] && rm -f -- "$f" 2>/dev/null || true
  done
}
trap cleanup EXIT
scratch_file() {
  local f
  f="$(mktemp "${TMPDIR:-/tmp}/bnotify.XXXXXX")" || return 1
  chmod 600 "$f" || true
  TMP_FILES+=("$f")
  printf '%s' "$f"
}
urlenc() { jq -rn --arg s "$1" '$s|@uri'; }

# mx_req <METHOD> <path> [json-body-file] — authenticated request; sets
# MX_CODE (000 on transport failure) and MX_BODY.
mx_req() {
  local method="$1" path="$2" body_file="${3:-}" out args=()
  if [[ -z "$MX_AUTH_FILE" ]]; then
    err "mx_req called before login (programming error)"
    return 1
  fi
  args=(--silent --show-error --compressed --max-time 30
        -X "$method" -H "@${MX_AUTH_FILE}"
        -H 'Content-Type: application/json')
  if [[ -n "$body_file" ]]; then
    args+=(--data-binary "@${body_file}")
  fi
  MX_BODY=""
  MX_CODE="000"
  out="$(curl "${args[@]}" "${MATRIX_HOMESERVER}${path}" -w '\n%{http_code}' 2>/dev/null)" \
    || { MX_CODE="000"; return 1; }
  MX_CODE="${out##*$'\n'}"
  MX_BODY="${out%$'\n'*}"
  [[ "$MX_CODE" =~ ^2 ]] && return 0
  return 1
}

# mx_login — password login (m.login.password); sets MX_TOKEN. The
# credentials travel via a 0600 temp file, the token never leaves vars.
# One 429 (M_LIMIT_EXCEEDED) retry with a short backoff — repeated
# passes must not fail just because a previous pass logged in recently.
mx_login() {
  local f resp attempt code
  f="$(scratch_file)" || { err "no scratch file for login body"; return 1; }
  jq -n --arg user "$MATRIX_USER" --arg pass "$MATRIX_PASSWORD" \
    '{type:"m.login.password",
      identifier:{type:"m.id.user",user:$user},
      password:$pass}' >"$f"
  resp=""
  code="000"
  for attempt in 1 2 3; do
    resp="$(curl --silent --show-error --max-time 30 \
             -H 'Content-Type: application/json' --data-binary "@${f}" \
             "${MATRIX_HOMESERVER}/_matrix/client/v3/login" \
             -w '\n%{http_code}' 2>/dev/null)" || resp=""
    code="${resp##*$'\n'}"
    if [[ "$code" != "429" || "$attempt" == "3" ]]; then
      break
    fi
    # honor the server's retry hint, fall back to linear backoff (cap 60s)
    sleep_s="$(jq -r '((.retry_after_ms // 5000) / 1000 + 2) | floor' <<<"${resp%$'\n'*}" 2>/dev/null || true)"
    if ! [[ "$sleep_s" =~ ^[0-9]+$ ]]; then
      sleep_s=$((attempt * 10))
    fi
    if [[ "$sleep_s" -gt 60 ]]; then
      sleep_s=60
    fi
    info "login rate-limited (429) — retrying in ${sleep_s}s"
    sleep "$sleep_s"
  done
  MX_CODE="$code"
  MX_BODY="${resp%$'\n'*}"
  MX_TOKEN="$(jq -r '.access_token // empty' <<<"$MX_BODY")"
  if [[ -z "$MX_TOKEN" ]]; then
    err "login failed for '${MATRIX_USER}' (HTTP ${MX_CODE}, errcode: $(jq -r '.errcode // "unknown"' <<<"$MX_BODY" 2>/dev/null))"
    return 1
  fi
  MX_AUTH_FILE="$(scratch_file)" || { err "no scratch file for auth header"; return 1; }
  printf 'Authorization: Bearer %s\n' "$MX_TOKEN" >"$MX_AUTH_FILE"
  return 0
}

# create_room <alias-localpart> — create the support room (public, so
# support staff can join via the room directory / alias) and invite any
# MATRIX_ROOM_INVITE mxids; sets ROOM_ID.
create_room() {
  local alias_localpart="$1" f body inv
  body="$(jq -n --arg a "$alias_localpart" \
    '{room_alias_name:$a, name:"Support",
      topic:"calrs booking notifications (automated)",
      visibility:"public", preset:"public_chat"}')"
  if [[ -n "$MATRIX_ROOM_INVITE" ]]; then
    inv="$(printf '%s' "$MATRIX_ROOM_INVITE" \
      | jq -R 'split(",")|map(gsub("^ +| +$";""))|map(select(length>0))')"
    body="$(jq -n --argjson base "$body" --argjson invite "$inv" '$base + {invite:$invite}')"
  fi
  f="$(scratch_file)" || { err "no scratch file for createRoom body"; return 1; }
  printf '%s' "$body" >"$f"
  if mx_req POST "/_matrix/client/v3/createRoom" "$f"; then
    ROOM_ID="$(jq -r '.room_id // empty' <<<"$MX_BODY")"
    if [[ -n "$ROOM_ID" ]]; then
      ok "created room ${alias_localpart} (id ${ROOM_ID})"
      return 0
    fi
  fi
  err "room creation failed (HTTP ${MX_CODE}: $(jq -r '.error // "unknown"' <<<"$MX_BODY" 2>/dev/null))"
  return 1
}

# room_id_for <create-missing:yes|no> — resolve MATRIX_ROOM ('!'id or
# '#'alias) to a room id; sets ROOM_ID. With yes, a missing default alias
# room is created via the demo-user credentials (task provisioning path).
room_id_for() {
  local create="$1" r="$MATRIX_ROOM" enc localpart errcode
  if [[ "$r" == '!'* ]]; then
    ROOM_ID="$r"
    return 0
  fi
  if [[ "$r" != '#'* ]]; then
    err "unsupported MATRIX_ROOM '${r}' (use '!room_id' or '#alias')"
    return 1
  fi
  enc="$(urlenc "$r")"
  if mx_req GET "/_matrix/client/v3/directory/room/${enc}"; then
    ROOM_ID="$(jq -r '.room_id // empty' <<<"$MX_BODY")"
    [[ -n "$ROOM_ID" ]] && return 0
  fi
  errcode="$(jq -r '.errcode // empty' <<<"$MX_BODY" 2>/dev/null)"
  if [[ "$errcode" != "M_NOT_FOUND" ]]; then
    err "alias lookup failed for ${r} (HTTP ${MX_CODE})"
    return 1
  fi
  if [[ "$create" != "yes" ]]; then
    return 1   # caller decides how to report a missing room
  fi
  # only auto-create aliases that live on OUR homeserver
  if [[ "$r" != *":${MATRIX_DOMAIN}" ]]; then
    err "room ${r} does not exist and auto-create only supports '*:${MATRIX_DOMAIN}' aliases"
    return 1
  fi
  localpart="${r#\#}"
  localpart="${localpart%%:*}"
  create_room "$localpart"
}

# ensure_joined — join ROOM_ID if not already a member (creator auto-joins)
ensure_joined() {
  local joined
  joined="$(mx_req GET "/_matrix/client/v3/joined_rooms" \
    && jq -r --arg id "$ROOM_ID" '(.joined_rooms // []) | index($id) != null' <<<"$MX_BODY")" \
    || joined="false"
  [[ "$joined" == "true" ]] && { ok "joined ${ROOM_ID}"; return 0; }
  if mx_req POST "/_matrix/client/v3/join/$(urlenc "$ROOM_ID")"; then
    ok "joined ${ROOM_ID}"
    return 0
  fi
  err "cannot join ${ROOM_ID} (HTTP ${MX_CODE}: $(jq -r '.error // "unknown"' <<<"$MX_BODY" 2>/dev/null))"
  return 1
}

# send_booking <booking-id> — POST the notification as m.room.message.
# PUT with the booking id as txnId makes retries idempotent (server-side
# dedupe); body travels via a 0600 temp file.
send_booking() {
  local bid="$1" msg f
  msg="$(booking_message "$2")"
  f="$(scratch_file)" || { err "no scratch file for message body"; return 1; }
  jq -n --arg body "$msg" '{msgtype:"m.text", body:$body}' >"$f"
  if mx_req PUT "/_matrix/client/v3/rooms/$(urlenc "$ROOM_ID")/send/m.room.message/bnotify-$(urlenc "$bid")" "$f"; then
    printf '%s\n' "  event $(jq -r '.event_id // "?"' <<<"$MX_BODY")"
    return 0
  fi
  err "send failed for booking ${bid} (HTTP ${MX_CODE})"
  return 1
}

# booking_message <json-row> — render the notification body
booking_message() {
  local row="$1" name email tz start event
  name="$(jq -r '.name' <<<"$row")"
  email="$(jq -r '.email' <<<"$row")"
  tz="$(jq -r '.tz' <<<"$row")"
  start="$(jq -r '.start' <<<"$row")"
  event="$(jq -r '.event // ""' <<<"$row")"
  printf '📅 New calrs booking confirmed\nGuest: %s <%s>\nEvent: %s\nWhen: %s (%s)\nBooking: %s\n' \
    "$name" "$email" "${event:-—}" "$start" "$tz" "$(jq -r '.id' <<<"$row")"
}

# pending_bookings — print confirmed bookings (JSON lines) that are NOT
# yet recorded in the state file. Read-only; fails when the state file
# or the db is unreadable (never reports an empty pending set on UNKNOWN).
pending_bookings() {
  local state_tmp row id state_raw
  state_tmp="$(scratch_file)" || return 1
  : >"$state_tmp"
  state_raw="$(state_ids)" || { warn "state file unreadable — cannot determine the pending set"; return 1; }
  # `|| true` guards: under pipefail, grep selects-zero-lines (rc 1) and a
  # final `[[ ]] &&` body (rc 1) would poison the pipeline and trip set -e
  printf '%s\n' "$state_raw" | { grep -v '^#' || true; } | {
    while IFS= read -r id; do
      if [[ -n "$id" ]]; then
        printf '%s\n' "$id" >>"$state_tmp"
      fi
    done
  }
  while IFS= read -r row; do
    if [[ -z "$row" ]]; then
      continue
    fi
    id="$(jq -r '.id' <<<"$row")"
    if ! grep -Fxq "$id" "$state_tmp"; then
      printf '%s\n' "$row"
    fi
  done < <(db_bookings)
}

# ensure_login — reuse a validated access token across passes (loop mode
# logs in once per process instead of once per pass: avoids Synapse's
# login rate limit and device-per-login proliferation). A dead token
# falls through to a fresh password login.
ensure_login() {
  if [[ -n "$MX_TOKEN" ]] && mx_req GET "/_matrix/client/v3/account/whoami" \
     && [[ "$(jq -r '.user_id // empty' <<<"$MX_BODY")" == "@${MATRIX_USER}:${MATRIX_DOMAIN}" ]]; then
    return 0
  fi
  mx_login
}

# run_pass — one apply pass: login, ensure room, notify pending bookings,
# record ids. Non-zero only on hard failures (login/room/db) or if at
# least one send failed.
run_pass() {
  local posted=0 failed=0 row id
  if [[ -z "$(volume_of)" ]]; then
    err "cannot resolve the calrs-data volume (is ${CALENDAR_CONTAINER} running?)"
    return 1
  fi
  ensure_login || return 1
  room_id_for yes || return 1
  ensure_joined || return 1

  # state readability is load-bearing: unknown state must never post
  if ! state_ids >/dev/null 2>&1; then
    warn "state file unreadable on the calrs-data volume — skipping this pass to avoid duplicate posts"
    return 1
  fi

  # db readability is load-bearing: never post on UNKNOWN
  if [[ -z "$(db_bookings)" ]]; then
    warn "cannot read calrs bookings (sqlite helper failed) — skipping this pass"
    return 1
  fi

  while IFS= read -r row; do
    [[ -z "$row" ]] && continue
    id="$(jq -r '.id' <<<"$row")"
    info "posting booking ${id}"
    if send_booking "$id" "$row"; then
      if state_add "$id"; then
        posted=$((posted + 1))
      else
        warn "posted, but could not record ${id} — it may re-post next pass"
        posted=$((posted + 1))
      fi
    else
      failed=$((failed + 1))
    fi
  done < <(pending_bookings)

  info "pass complete: ${posted} posted, ${failed} failed"
  [[ "$failed" -eq 0 ]]
}

# cmd_dry_run — print exactly what WOULD be posted; strictly read-only,
# no login, no mutation; always exits 0.
cmd_dry_run() {
  local pending
  info "dry-run — homeserver: ${MATRIX_HOMESERVER}"
  info "user: ${MATRIX_USER} (login suppressed in dry-run)"
  if [[ "$MATRIX_ROOM" == '!'* ]]; then
    info "room: ${MATRIX_ROOM}"
  elif mx_code_probe_alias "$MATRIX_ROOM" "200"; then
    info "room: ${MATRIX_ROOM} (exists, id $(alias_room_id "$MATRIX_ROOM"))"
  else
    info "room: ${MATRIX_ROOM} — MISSING, would be created (public, preset public_chat, alias on ${MATRIX_DOMAIN})"
  fi
  if [[ -z "$(volume_of)" ]]; then
    warn "calrs-data volume unresolved — cannot read bookings in dry-run"
  fi
  pending="$(pending_bookings)"
  if [[ -z "$(db_bookings)" ]]; then
    warn "cannot read calrs bookings (sqlite helper failed) — showing 0 pending"
  elif [[ -z "$pending" ]]; then
    ok "0 pending bookings — nothing would be posted"
  else
    info "would post $(printf '%s\n' "$pending" | grep -c .) notification(s):"
    while IFS= read -r row; do
      [[ -z "$row" ]] && continue
      printf '  ── booking %s ──\n' "$(jq -r '.id' <<<"$row")"
      booking_message "$row" | sed 's/^/  │ /'
    done <<<"$pending"
  fi
  return 0
}

# alias probes without auth (room directory lookups are unauthenticated)
mx_code_probe_alias() {
  local enc
  enc="$(urlenc "$1")"
  MX_BODY=""
  MX_CODE="000"
  MX_CODE="$(curl --silent --max-time 15 \
    "${MATRIX_HOMESERVER}/_matrix/client/v3/directory/room/${enc}" \
    -o /dev/null -w '%{http_code}' 2>/dev/null)" || MX_CODE="000"
  [[ "$MX_CODE" == "$2" ]]
}
alias_room_id() {
  local enc
  enc="$(urlenc "$1")"
  curl --silent --max-time 15 \
    "${MATRIX_HOMESERVER}/_matrix/client/v3/directory/room/${enc}" 2>/dev/null \
    | jq -r '.room_id // "?"' 2>/dev/null || printf '?'
}

# cmd_check — read-only configuration + connectivity audit
CHECKS_FAILED=0
check() {  # check <label> <cmd...>
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    ok "check: ${label}"
  else
    err "check: ${label} — FAILED"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
  fi
}
cmd_check() {
  local pending_count logged_in=false
  info "check — homeserver: ${MATRIX_HOMESERVER}, user: ${MATRIX_USER}, room: ${MATRIX_ROOM}"
  check "volume resolved (container ${CALENDAR_CONTAINER})" volume_of_nonempty
  check "booking db readable (sqlite helper)" db_has_rows
  check "state file writable on calrs-data volume" state_probe
  check "homeserver reachable (/_matrix/client/versions)" hs_versions
  if mx_login_quiet; then
    ok "check: login (password auth)"
    logged_in=true
  else
    err "check: login (password auth) — FAILED"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
  fi
  if [[ "$logged_in" == true ]]; then
    if room_id_for no; then
      ok "check: room ${MATRIX_ROOM} exists (id ${ROOM_ID})"
      check "joined to room" ensure_joined
    else
      err "check: room ${MATRIX_ROOM} — MISSING (apply would create it)"
      CHECKS_FAILED=$((CHECKS_FAILED + 1))
    fi
  fi
  if [[ -n "$(volume_of)" ]] && [[ -n "$(db_bookings)" ]]; then
    pending_count="$(pending_bookings | grep -c . || true)"
    info "check: ${pending_count} pending booking(s) would be posted by the next apply pass"
  fi
  if [[ "$CHECKS_FAILED" -eq 0 ]]; then
    ok "all checks passed"
    return 0
  fi
  err "${CHECKS_FAILED} check(s) failed"
  return 1
}

# tiny quiet wrappers for check()
volume_of_nonempty() { [[ -n "$(volume_of)" ]]; }
db_has_rows() { [[ -n "$(db_bookings)" ]]; }
state_probe() {
  printf '%s\n' "probe-$(date +%s)" \
    | helper_sh rw "cat > /data/.booking-notify-probe && rm -f /data/.booking-notify-probe"
}
hs_versions() { mx_code_probe_path "/_matrix/client/versions" 200; }
mx_code_probe_path() {
  local path="$1" want="$2"
  local code
  code="$(curl --silent --max-time 15 "${MATRIX_HOMESERVER}${path}" \
    -o /dev/null -w '%{http_code}' 2>/dev/null)" || code="000"
  [[ "$code" == "$want" ]]
}
mx_login_quiet() { mx_login; }

# ── main ────────────────────────────────────────────────────────────────────
CALRS_VOLUME="$(volume_of)"
if [[ -n "$CALRS_VOLUME" ]]; then
  info "calrs-data volume: ${CALRS_VOLUME}"
elif [[ "$MODE" != "dry-run" ]]; then
  warn "calrs-data volume unresolved — apply/check passes will fail until ${CALENDAR_CONTAINER} runs"
fi

if [[ "$LOOP_MODE" == true ]]; then
  info "loop mode: one apply pass every ${LOOP_INTERVAL}s (Ctrl-C to stop)"
  trap 'exit 0' INT TERM
  while :; do
    run_pass || warn "pass failed — retrying in ${LOOP_INTERVAL}s"
    sleep "$LOOP_INTERVAL" &
    wait "$!" || true
  done
fi

case "$MODE" in
  dry-run) cmd_dry_run ;;
  check)   cmd_check ;;
  apply)   run_pass ;;
esac
