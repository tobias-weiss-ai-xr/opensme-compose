#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
# calendar-setup.sh — IDEMPOTENT bootstrap of the calendar stack
# (calrs public booking + its dedicated Radicale CalDAV store)
#
# Brings up the adopted calendar services (calendar + calendar-radicale
# in the main docker-compose.yml) and seeds every piece of state the
# compose overlay cannot express declaratively:
#
#   1. Radicale htpasswd  — the booking CalDAV login (RADICALE_USER /
#      RADICALE_PASSWORD) is bcrypt-hashed into /config/users of the
#      radicale-config volume via a one-shot httpd:2.4-alpine helper
#      container (the tomsquest/docker-radicale image ships no
#      htpasswd(1)). Skipped when the user is already in the file.
#   2. calendar-radicale is restarted ONLY when the htpasswd changed,
#      so the auth file is deterministically picked up.
#   3. `docker compose up -d --wait calendar` — the `calendar` profile
#      is always enabled; service names, not container names.
#   4. calrs admin user   — `calrs user create --email --name --admin`
#      (the first user is admin by construction); skipped when the
#      email already shows up in `calrs user list`.
#   5. CalDAV source      — `calrs source add` pointing calrs at its
#      radicale over opensme-net (http://calendar-radicale:5232/,
#      container DNS name = compose service name); skipped when the
#      URL is already connected.
#   6. `calrs sync`       — `--full` on first source add (and with
#      --full), incremental otherwise. This step is the real
#      credential check for the seeded htpasswd user.
#   7. Public event type  — `calrs event-type create` for the public
#      "Video Consultation" booking page (slug `vc`, 30 min, VC link
#      from CALRS_VC_URL); skipped when the slug exists.
#   8. Prints the public booking URL + a one-line privacy reminder.
#
# calrs v1.6.0 CLI surface — VERIFIED against the pinned upstream tag
# (github.com/olivierlambert/calrs v1.6.0, src/commands/*.rs); do NOT
# "fix" the invocations below from memory:
#   * `calrs user create` takes ONLY --email/--name/--admin — the
#     password is an rpassword prompt that reads /dev/tty EXCLUSIVELY
#     (no stdin fallback; upstream .unwrap_or_default()s the error).
#     Piping into `docker compose exec -T` therefore yields an EMPTY
#     password — password-feeding commands run behind an expect-style
#     python3 pty feeder that waits for the "Password:" prompt, turns
#     ECHO off on the pty and then types the password (stdin only,
#     never argv, never echoed).
#   * `calrs source add` has NO --password flag (same rpassword prompt)
#     and no duplicate guard upstream — the `calrs source list` check
#     below IS the idempotency. --no-test keeps provisioning
#     deterministic; the sync step validates the credentials for real.
#   * `calrs event-type create` has NO location/visibility flags in
#     v1.6.0: the schema defaults new types to location_type='link'
#     (empty location_value) and visibility='public', and auto-creates
#     Mon-Fri 09:00-17:00 availability. IMPORTANT (verified live): the
#     VC URL in --description is NEVER rendered to guests — the booking
#     confirmation shows a clickable join link only when
#     event_types.location_value is set (seed it via the dashboard or a
#     direct UPDATE).
#   * Booking pages live at /u/<username>/<slug>; the username is the
#     lowercased email local part with '.'->'-' (src/auth.rs
#     generate_username).
#
# Adopted into the core stack (2026-11-07): the calendar services
# (calendar + calendar-radicale) live in the main docker-compose.yml
# and are always included — no overlay, no --profile needed. This
# script targets the main compose (COMPOSE_FILE env / --compose-file
# override is honored; default docker-compose.yml). A legacy chain that
# still lists services/calendar.yml parses fine (it's an empty shim).
#
# Secrets: RADICALE_PASSWORD / CALRS_ADMIN_PASSWORD come from the env
# file / environment, are piped on stdin and are NEVER echoed, logged
# or passed as a child argv value. xtrace stays off (defensively, even
# when inherited via SHELLOPTS).
#
# Usage:
#   scripts/calendar-setup.sh                                  # .env in repo root
#   scripts/calendar-setup.sh --env-file /path/to/env          # alternate env file
#   scripts/calendar-setup.sh --compose-file "docker-compose.yml"
#   scripts/calendar-setup.sh --full                           # force a full re-sync
#   scripts/calendar-setup.sh --help
#
# Requires: docker (compose v2.20+), jq, plus python3 (the repo baseline
# per CONTRIBUTING.md) for the password prompts. Rotating RADICALE_PASSWORD
# later also requires re-adding the CalDAV source (dashboard -> sources,
# or `calrs source remove`) — the stored source password is encrypted.
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail
set +x   # never trace: secrets flow through the pipes below

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC}  $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1

usage() {
  cat <<'EOF'
Usage: scripts/calendar-setup.sh [options]

Idempotent bootstrap of the calendar stack (calrs + calendar-radicale
from the core stack): seeds the Radicale htpasswd user, creates
the calrs admin, connects the CalDAV source, runs a full sync, creates
the public 'Video Consultation' event type and prints the booking URL.

Options:
  --env-file PATH      env file to load (default: .env in the repo root)
  --compose-file LIST  colon-separated compose chain (default: $COMPOSE_FILE
                       or docker-compose.yml — the calendar services live in
                       the main compose since 2026-11-07 adoption)
  --full               force `calrs sync --full` even when unchanged
  --help, -h           show this help

Environment (from the env file or the environment):
  RADICALE_USER / RADICALE_PASSWORD   booking CalDAV login (htpasswd, bcrypt)
  CALRS_ADMIN_EMAIL / CALRS_ADMIN_PASSWORD  calrs admin (first user = admin)
  CALRS_VC_URL                        video-consultation join link (optional)
  CALENDAR_DOMAIN / CALRS_IMAGE_TAG   calendar host / image tag (compose)

Requires: docker, jq, python3 (CONTRIBUTING baseline) — python3 feeds
the calrs password prompts through a pty.
EOF
}

# ── arguments ───────────────────────────────────────────────────────────────
ENV_FILE=".env"
COMPOSE_FILE_OVERRIDE=""
FORCE_FULL=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)     [[ -n "${2:-}" ]] || { err "--env-file requires a value"; exit 1; }
                    ENV_FILE="$2"; shift 2 ;;
    --compose-file) [[ -n "${2:-}" ]] || { err "--compose-file requires a value"; exit 1; }
                    COMPOSE_FILE_OVERRIDE="$2"; shift 2 ;;
    --full)         FORCE_FULL=true; shift ;;
    --help|-h)      usage; exit 0 ;;
    *)              err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

for tool in docker jq; do
  command -v "$tool" >/dev/null 2>&1 || { err "Required tool not found: ${tool}"; exit 1; }
done

# ── env file (repo .env or --env-file arg) ─────────────────────────────────
# explicit environment beats the env file (docker semantics); capture the
# operator's COMPOSE_FILE BEFORE sourcing so an exported value wins
COMPOSE_FILE_FROM_ENV="${COMPOSE_FILE:-}"
if [[ ! -f "$ENV_FILE" ]]; then
  err "env file '${ENV_FILE}' not found (generate one with scripts/init-env.sh or pass --env-file)"
  exit 1
fi
set -a
# shellcheck source=/dev/null  # generated at deploy time
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# ── compose chain (COMPOSE_FILE override supported) ──────────────────────
COMPOSE_FILE="${COMPOSE_FILE_OVERRIDE:-${COMPOSE_FILE_FROM_ENV:-docker-compose.yml}}"
COMPOSE_FILE="${COMPOSE_FILE%$'\r'}"
export COMPOSE_FILE

COMPOSE_ARGS=(--env-file "$ENV_FILE")
IFS=':' read -r -a COMPOSE_FILES <<< "$COMPOSE_FILE"
for f in "${COMPOSE_FILES[@]}"; do
  if [[ -n "$f" ]]; then
    COMPOSE_ARGS+=(-f "$f")
  fi
done

dc() { docker compose "${COMPOSE_ARGS[@]}" "$@"; }

# read-only calrs listing (no password prompt involved)
calrs_list() { docker compose "${COMPOSE_ARGS[@]}" exec -T calendar calrs "$@"; }

# feed_stdin <password> <command...> — run COMMAND in a pty and answer
# the first "Password:" prompt with <password>.
# calrs's rpassword prompts read /dev/tty ONLY (see header): a plain
# `exec -T` pipe yields an empty password, and blind-writing into a pty
# would be ECHOed back by the line discipline before the consumer turns
# echo off (rpassword prints the prompt first). The feeder therefore
# waits for the prompt string, force-disables ECHO on the pty and only
# then types the password. The password travels on stdin, never in argv.
feed_stdin() {
  local payload="$1"; shift
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$payload" | python3 -c '
import os, pty, select, sys, time, termios
pw = sys.stdin.readline().rstrip("\n")
cmd = sys.argv[1:]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)
sent = False
deadline = time.monotonic() + 120
while True:
    if time.monotonic() > deadline:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
        sys.exit(124)
    try:
        r, _, _ = select.select([fd], [], [], 0.5)
    except OSError:
        break
    if fd in r:
        try:
            data = os.read(fd, 4096)
        except OSError:
            data = b""
        if not data:
            break
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        if not sent and b"assword" in data:
            try:
                attrs = termios.tcgetattr(fd)
                attrs[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
            except Exception:
                pass
            time.sleep(0.2)
            os.write(fd, pw.encode() + b"\r")
            sent = True
    wpid, status = os.waitpid(pid, os.WNOHANG)
    if wpid == pid:
        try:
            while True:
                r, _, _ = select.select([fd], [], [], 0.2)
                if fd not in r:
                    break
                tail = os.read(fd, 4096)
                if not tail:
                    break
                sys.stdout.buffer.write(tail)
        except OSError:
            pass
        sys.stdout.buffer.flush()
        sys.exit(os.waitstatus_to_exitcode(status))
# EOF on the master (child closed the slave) — reap and pass its status on
_, status = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(status))
' "$@"
    return
  fi
  warn "python3 not found — cannot answer calrs password prompts safely; trying a direct pipe"
  printf '%s\n' "$payload" | "$@"
}

# ── configuration ───────────────────────────────────────────────────────────
CALENDAR_DOMAIN="${CALENDAR_DOMAIN:-calendar.${OPENSME_DOMAIN:-opensme.graphwiz.ai}}"
CALENDAR_DOMAIN="${CALENDAR_DOMAIN%$'\r'}"
CALRS_IMAGE_TAG="${CALRS_IMAGE_TAG:-1.6.0}"
CALRS_ADMIN_EMAIL="${CALRS_ADMIN_EMAIL%$'\r'}"

if [[ -z "${RADICALE_USER:-}" || -z "${RADICALE_PASSWORD:-}" ]]; then
  err "RADICALE_USER / RADICALE_PASSWORD must be set (see .env.example, calendar block)"
  exit 1
fi
if [[ -z "${CALRS_ADMIN_EMAIL:-}" ]]; then
  err "CALRS_ADMIN_EMAIL must be set (see .env.example, calendar block)"
  exit 1
fi
if [[ -z "${CALRS_ADMIN_PASSWORD:-}" ]]; then
  if [[ -t 0 ]]; then
    warn "CALRS_ADMIN_PASSWORD is not set — the admin password will be prompted interactively"
  else
    err "CALRS_ADMIN_PASSWORD must be set for non-interactive runs"
    exit 1
  fi
elif [[ "${#CALRS_ADMIN_PASSWORD}" -lt 8 ]]; then
  err "CALRS_ADMIN_PASSWORD is shorter than 8 characters (calrs minimum) — refusing to run"
  exit 1
fi
if [[ -z "${CALRS_VC_URL:-}" ]]; then
  warn "CALRS_VC_URL is empty — the event type will be created without a video-call link"
fi

# ── Step 1/8: Radicale htpasswd (helper container, /config/users) ──────────
radicale_volume() {
  local vol project
  vol="$(dc config --format json 2>/dev/null \
    | jq -r '.volumes["radicale-config"].name // empty' 2>/dev/null || true)"
  if [[ -z "$vol" ]]; then
    # named volume name not exposed — derive from the compose project name
    # (Docker's projection for an unnamed `radicale-config:` volume):
    # 1) explicit COMPOSE_PROJECT_NAME, 2) the project from compose config,
    # 3) cwd basename as a last resort.
    project="${COMPOSE_PROJECT_NAME:-}"
    if [[ -z "$project" ]]; then
      project="$(dc config --format json 2>/dev/null \
        | jq -r '.name // empty' 2>/dev/null || true)"
    fi
    vol="${project:-opensme-compose}_radicale-config"
    # stderr: this function's stdout is captured — a warn there would
    # pollute the volume name with ANSI codes and newlines
    warn "could not resolve the radicale-config volume via compose config — using '${vol}'" >&2
  fi
  printf '%s' "$vol"
}
RADICALE_VOLUME="$(radicale_volume)"

info "Step 1/8 — Radicale htpasswd user '${RADICALE_USER}' (volume: ${RADICALE_VOLUME})"
USERS_CHANGED=false
existing_users="$(docker run --rm -v "${RADICALE_VOLUME}":/config httpd:2.4-alpine \
  sh -c 'cat /config/users 2>/dev/null || true' 2>/dev/null || true)"
if printf '%s\n' "$existing_users" | grep -q "^${RADICALE_USER}:"; then
  ok "Radicale user '${RADICALE_USER}' already present — nothing to do"
else
  info "seeding '${RADICALE_USER}' with a bcrypt hash into /config/users (httpd:2.4-alpine helper)"
  set +x   # password rides stdin — never trace
  printf '%s\n' "$RADICALE_PASSWORD" | docker run --rm -i \
    -v "${RADICALE_VOLUME}":/config -e HTPASSWD_USER="${RADICALE_USER}" \
    httpd:2.4-alpine sh -c 'umask 022; touch /config/users;
      if [ -s /config/users ] && [ -n "$(tail -c 1 /config/users)" ]; then printf "\n" >> /config/users; fi;
      # htpasswd signature: <flags> PASSWORDFILE USERNAME. -B bcrypt, -i
      # reads the password from stdin (never argv) — the file is written
      # IN PLACE, no redirect. Do NOT pass -b (argv) together with -i
      # (stdin): the pair contradicts and htpasswd dumps usage.
      htpasswd -B -i /config/users "$HTPASSWD_USER"'
  if docker run --rm -v "${RADICALE_VOLUME}":/config httpd:2.4-alpine \
      sh -c 'cat /config/users 2>/dev/null || true' | grep -q "^${RADICALE_USER}:"; then
    ok "Radicale user '${RADICALE_USER}' seeded"
    USERS_CHANGED=true
  else
    err "htpasswd seeding failed — '${RADICALE_USER}' not found in /config/users afterwards"
    exit 1
  fi
fi

# ── Step 2/8: restart calendar-radicale only when the users file changed ───
if [[ "$USERS_CHANGED" == true ]]; then
  info "Step 2/8 — restarting calendar-radicale to pick up the new htpasswd"
  dc restart calendar-radicale 2>/dev/null \
    || info "calendar-radicale not running yet — it reads /config/users at first start"
else
  info "Step 2/8 — htpasswd unchanged, calendar-radicale keeps running"
fi

# ── Step 3/8: bring the calendar services up ────────────────────────────────
info "Step 3/8 — docker compose up -d --wait calendar (chain: ${COMPOSE_FILE})"
if ! docker image inspect "calrs:${CALRS_IMAGE_TAG}" >/dev/null 2>&1; then
  err "image calrs:${CALRS_IMAGE_TAG} not found — compose would try to BUILD images/calrs,"
  err "which is a pinned reference, not a self-building subtree. Build the image from the"
  err "upstream tag first (images/calrs/README.md): clone, checkout v${CALRS_IMAGE_TAG},"
  err "docker buildx build -t calrs:${CALRS_IMAGE_TAG} ."
  exit 1
fi
dc up -d --wait calendar

# ── Step 4/8: calrs admin user ──────────────────────────────────────────────
info "Step 4/8 — calrs admin user (${CALRS_ADMIN_EMAIL})"
if calrs_list user list 2>/dev/null | grep -Fq "$CALRS_ADMIN_EMAIL"; then
  ok "calrs user '${CALRS_ADMIN_EMAIL}' already exists — skipping create"
else
  info "creating calrs admin (first user is admin by construction)"
  set +x   # password rides stdin (via the pty wrapper) — never trace
  if [[ -n "${CALRS_ADMIN_PASSWORD:-}" ]]; then
    if ! feed_stdin "$CALRS_ADMIN_PASSWORD" \
        docker compose "${COMPOSE_ARGS[@]}" exec calendar calrs user create \
          --email "$CALRS_ADMIN_EMAIL" --name "Calendar Admin" --admin; then
      # tolerate a create race / pty hiccup: re-check before dying
      if calrs_list user list 2>/dev/null | grep -Fq "$CALRS_ADMIN_EMAIL"; then
        ok "calrs user '${CALRS_ADMIN_EMAIL}' exists after all (create raced)"
      else
        err "calrs user create failed — check CALRS_ADMIN_PASSWORD (min 8 chars) and the calendar logs"
        exit 1
      fi
    fi
  else
    # interactive: the operator's terminal is the tty rpassword wants
    docker compose "${COMPOSE_ARGS[@]}" exec calendar calrs user create \
      --email "$CALRS_ADMIN_EMAIL" --name "Calendar Admin" --admin
  fi
fi

# ── Step 5/8: CalDAV source -> radicale ─────────────────────────────────────
CALDAV_URL="http://calendar-radicale:5232/"
info "Step 5/8 — CalDAV source '${CALDAV_URL}' (Public Calendar)"
SOURCE_ADDED=false
if calrs_list source list 2>/dev/null | grep -Fq "$CALDAV_URL"; then
  ok "CalDAV source '${CALDAV_URL}' already connected — skipping add"
else
  # v1.6.0 `calrs source add` has no --password flag: the password is a
  # rpassword prompt -> fed through the pty wrapper; --no-test keeps the
  # run deterministic (the sync below is the real credential check).
  set +x   # password rides stdin (via the pty wrapper) — never trace
  if feed_stdin "$RADICALE_PASSWORD" \
      docker compose "${COMPOSE_ARGS[@]}" exec calendar calrs source add \
        --name "Public Calendar" --url "$CALDAV_URL" \
        --username "$RADICALE_USER" --no-test; then
    SOURCE_ADDED=true
  elif calrs_list source list 2>/dev/null | grep -Fq "$CALDAV_URL"; then
    ok "CalDAV source '${CALDAV_URL}' exists after all (add raced)"
    SOURCE_ADDED=true
  else
    err "calrs source add failed — check the calendar logs"
    exit 1
  fi
fi

# ── Step 6/8: sync (full on first source add) ───────────────────────────────
info "Step 6/8 — calrs sync"
if [[ "$SOURCE_ADDED" == true || "$FORCE_FULL" == true ]]; then
  if ! docker compose "${COMPOSE_ARGS[@]}" exec -T calendar calrs sync --full; then
    err "full sync failed — usually a Radicale auth problem: verify RADICALE_USER/"
    err "RADICALE_PASSWORD, and remember a rotated password needs the source re-added"
    exit 1
  fi
else
  if ! docker compose "${COMPOSE_ARGS[@]}" exec -T calendar calrs sync; then
    err "sync failed — usually a Radicale auth problem: verify RADICALE_USER/"
    err "RADICALE_PASSWORD, and remember a rotated password needs the source re-added"
    exit 1
  fi
fi

# ── Step 7/8: public 'Video Consultation' event type ────────────────────────
info "Step 7/8 — public 'Video Consultation' event type (slug vc)"
# v1.6.0 has no location/visibility flags: new types default to
# visibility='public' + location_type='link' (empty location_value) and
# Mon-Fri 09:00-17:00 availability — the VC link rides in the description.
if calrs_list event-type list 2>/dev/null | grep -Eq '(^|[[:space:]|])vc([[:space:]|]|$)'; then
  ok "event type 'vc' already exists — skipping create"
else
  if [[ -n "${CALRS_VC_URL:-}" ]]; then
    CALRS_VC_URL="${CALRS_VC_URL%$'\r'}"
    docker compose "${COMPOSE_ARGS[@]}" exec -T calendar calrs event-type create \
      --title "Video Consultation" --slug vc --duration 30 \
      --description "Video consultation. Join link: ${CALRS_VC_URL}"
  else
    docker compose "${COMPOSE_ARGS[@]}" exec -T calendar calrs event-type create \
      --title "Video Consultation" --slug vc --duration 30
  fi
fi

# ── Step 8/8: booking URL ───────────────────────────────────────────────────
# /u/<username>/<slug>; username = lowercased email local part, '.'->'-',
# everything but [a-z0-9-] dropped (calrs src/auth.rs generate_username)
booking_user="$(printf '%s' "$CALRS_ADMIN_EMAIL" | cut -d@ -f1 \
  | tr '[:upper:]' '[:lower:]' | tr '.' '-' | tr -cd 'a-z0-9-')"
[[ -n "$booking_user" ]] || booking_user="user"
BOOKING_URL="https://${CALENDAR_DOMAIN}/u/${booking_user}/vc"

echo ""
ok "calendar stack seeded."
echo "  Booking page : ${BOOKING_URL}"
echo "  Admin UI     : https://${CALENDAR_DOMAIN}/dashboard (login: ${CALRS_ADMIN_EMAIL})"
echo "  Privacy      : the booking page is PUBLIC — anyone with the URL sees free slots;"
echo "                 event details and the VC link stay private to confirmed bookings."
