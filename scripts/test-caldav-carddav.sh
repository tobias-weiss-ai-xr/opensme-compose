#!/usr/bin/env bash
# ── openSME — CalDAV / CardDAV endpoint verification ───────────────────────
#
# Verifies that the OpenCloud (ocis) DAV endpoints respond correctly for
# calendar (CalDAV) and address-book (CardDAV) collections.
#
# CalDAV probes:
#   1. PROPFIND  /remote.php/dav/calendars/<user>/   (expect 207 Multi-Status)
#   2. OPTIONS   /remote.php/dav/calendars/          (expect 200 + DAV header)
#   3. PROPFIND  /remote.php/dav/principals/         (expect 207 Multi-Status)
#
# CardDAV probes:
#   4. PROPFIND  /remote.php/dav/addressbooks/<user>/ (expect 207 Multi-Status)
#   5. OPTIONS   /remote.php/dav/addressbooks/        (expect 200 + DAV header)
#
# Exits 0 only if every requested probe passes. Use --caldav-only or
# --carddav-only to restrict the suite; both default to running.
#
# Usage:
#   scripts/test-caldav-carddav.sh                              # both suites, no auth
#   scripts/test-caldav-carddav.sh --caldav-only                # CalDAV suite only
#   scripts/test-caldav-carddav.sh --carddav-only               # CardDAV suite only
#   scripts/test-caldav-carddav.sh --with-credentials admin:s3cr3t
#   scripts/test-caldav-carddav.sh --base-url https://cloud.example.com
#   scripts/test-caldav-carddav.sh --caldav-only --with-credentials admin:s3cr3t \
#       --base-url https://cloud.example.com --user admin
#
# Options:
#   --caldav-only            Run only the CalDAV probes
#   --carddav-only           Run only the CardDAV probes
#   --with-credentials [U:P] Enable HTTP Basic auth. If the next token looks
#                            like a `user:pass` pair it is consumed; otherwise
#                            credentials are read from CLOUD_USER / CLOUD_PASS
#   --base-url URL           OpenCloud base URL (default: https://$OPENCLOUD_DOMAIN)
#   --user USER              Username for the per-user collection path
#                            (default: credential user, then "admin")
#   --help, -h               Show this help
#
# Environment:
#   OPENCLOUD_DOMAIN   OpenCloud host domain (default: cloud.$OPENSME_DOMAIN)
#   OPENSME_DOMAIN     Base domain (default: opensme.graphwiz.ai)
#   OPENCLOUD_BASE_URL Full base URL (overrides --base-url if unset)
#   CLOUD_USER         Basic-auth user (when --with-credentials has no value)
#   CLOUD_PASS         Basic-auth password (when --with-credentials has no value)
#
# ── Equivalent curl examples (copy/paste to run manually) ───────────────────
#
# CalDAV:
#   # List a user's calendar collections (Multi-Status 207)
#   curl -si -X PROPFIND \
#     -H "Content-Type: application/xml" \
#     -H "Depth: 1" \
#     --data '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:displayname/></d:prop></d:propfind>' \
#     -u admin:s3cr3t \
#     https://cloud.example.com/remote.php/dav/calendars/admin/
#
#   # Discover DAV/calendar capabilities (200 + DAV: 1, calendar-access header)
#   curl -si -X OPTIONS -u admin:s3cr3t \
#     https://cloud.example.com/remote.php/dav/calendars/
#
#   # List principals (Multi-Status 207)
#   curl -si -X PROPFIND \
#     -H "Content-Type: application/xml" \
#     -H "Depth: 1" \
#     --data '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>' \
#     -u admin:s3cr3t \
#     https://cloud.example.com/remote.php/dav/principals/
#
# CardDAV:
#   # List a user's address-book collections (Multi-Status 207)
#   curl -si -X PROPFIND \
#     -H "Content-Type: application/xml" \
#     -H "Depth: 1" \
#     --data '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:displayname/></d:prop></d:propfind>' \
#     -u admin:s3cr3t \
#     https://cloud.example.com/remote.php/dav/addressbooks/admin/
#
#   # Discover DAV/address-book capabilities (200 + DAV: 1, addressbook header)
#   curl -si -X OPTIONS -u admin:s3cr3t \
#     https://cloud.example.com/remote.php/dav/addressbooks/
#
# ════════════════════════════════════════════════════════════════════════════

set -uo pipefail

# ── color helpers ──────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
  BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; NC=''
fi
info() { echo -e "${BLUE}[DAV]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC}  $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1

# ── defaults ───────────────────────────────────────────────────────────────
DOMAIN="${OPENSME_DOMAIN:-opensme.graphwiz.ai}"
OPENCLOUD_DOMAIN="${OPENCLOUD_DOMAIN:-cloud.${DOMAIN}}"
BASE_URL="${OPENCLOUD_BASE_URL:-https://${OPENCLOUD_DOMAIN}}"
DAV_USER="${DAV_USER:-admin}"
AUTH_USER="${CLOUD_USER:-}"
AUTH_PASS="${CLOUD_PASS:-}"
USE_AUTH=false
RUN_CALDAV=true
RUN_CARDDAV=true
TIMEOUT=20

# ── argument parsing ───────────────────────────────────────────────────────
usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --caldav-only)
      RUN_CALDAV=true; RUN_CARDDAV=false; shift ;;
    --carddav-only)
      RUN_CALDAV=false; RUN_CARDDAV=true; shift ;;
    --with-credentials)
      USE_AUTH=true
      # Consume an optional `user:pass` value (next arg that is not a flag).
      if [[ $# -ge 2 && "$2" != --* ]]; then
        case "$2" in
          *:*)
            AUTH_USER="${2%%:*}"
            AUTH_PASS="${2#*:}"
            shift
            ;;
          *)
            # Bare username → read password from env CLOUD_PASS
            AUTH_USER="$2"
            shift
            ;;
        esac
      fi
      shift
      ;;
    --base-url)
      [[ $# -ge 2 ]] || { err "--base-url requires a URL"; exit 1; }
      BASE_URL="$2"; shift 2 ;;
    --user)
      [[ $# -ge 2 ]] || { err "--user requires a value"; exit 1; }
      DAV_USER="$2"; shift 2 ;;
    --timeout)
      [[ $# -ge 2 ]] || { err "--timeout requires a value"; exit 1; }
      TIMEOUT="$2"; shift 2 ;;
    --help|-h)
      usage; exit 0 ;;
    *)
      err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

# Strip any trailing slash from the base URL so path joins are predictable.
BASE_URL="${BASE_URL%/}"

# If credentials were supplied via flag, prefer the credential user for the
# per-user collection path unless --user was given explicitly.
if $USE_AUTH && [[ -n "$AUTH_USER" && -z "${DAV_USER_OVERRIDE:-}" ]]; then
  DAV_USER="$AUTH_USER"
fi

# Build the optional Basic-auth curl flags.
AUTH_FLAGS=()
if $USE_AUTH; then
  if [[ -z "$AUTH_USER" || -z "$AUTH_PASS" ]]; then
    err "--with-credentials requested but username/password incomplete. "
    err "Pass user:pass to --with-credentials or set CLOUD_USER and CLOUD_PASS."
    exit 1
  fi
  AUTH_FLAGS=(-u "${AUTH_USER}:${AUTH_PASS}")
fi

# Standard PROPFIND body used by the collection-listing probes.
PROPFIND_BODY='<?xml version="1.0" encoding="utf-8"?>'
PROPFIND_BODY+='<d:propfind xmlns:d="DAV:">'
PROPFIND_BODY+='<d:prop><d:resourcetype/><d:displayname/><d:current-user-principal/></d:prop>'
PROPFIND_BODY+='</d:propfind>'

# ── core test helper ───────────────────────────────────────────────────────
# dav_test NAME METHOD URL EXPECTED_PATTERN [HEADER_GREP]
# EXPECTED_PATTERN is an ERE matched against the HTTP status code (e.g. "207|200").
# HEADER_GREP (optional) is an ERE that must appear in the response headers
# (case-insensitive); used to confirm the DAV capability header on OPTIONS.
FAILS=0

dav_test() {
  local name="$1" method="$2" url="$3" pattern="$4" header_grep="${5:-}"
  local flags=(-s -o /dev/null -D - --max-time "$TIMEOUT" -X "$method" -w '\n%{http_code}\n')
  if [[ "$method" == "PROPFIND" ]]; then
    flags+=(-H "Content-Type: application/xml" -H "Depth: 1" --data "$PROPFIND_BODY")
  fi
  flags+=("${AUTH_FLAGS[@]}")

  local out code
  out="$(curl "${flags[@]}" "$url" 2>/dev/null)" || true
  code="$(printf '%s\n' "$out" | tail -n1)"

  if ! printf '%s' "$code" | grep -qE "^($pattern)\$"; then
    err "${name}: ${method} ${url} -> ${code} (expected ${pattern})"
    FAILS=$((FAILS + 1))
    return 1
  fi

  if [[ -n "$header_grep" ]]; then
    # Headers are everything except the trailing status line.
    local hdrs
    hdrs="$(printf '%s\n' "$out" | sed '$d')"
    if ! printf '%s' "$hdrs" | grep -qiE "$header_grep"; then
      err "${name}: ${method} ${url} -> ${code} but missing header matching /${header_grep}/"
      FAILS=$((FAILS + 1))
      return 1
    fi
  fi

  ok "${name}: ${method} ${url} -> ${code}"
  return 0
}

# ── CalDAV suite ───────────────────────────────────────────────────────────
run_caldav() {
  info "CalDAV endpoint probes (${BASE_URL})"
  # Without auth, ocis returns 401 on PROPFIND of a personal collection — that
  # still proves the endpoint exists and is DAV-aware. With auth we expect 207.
  local propfind_expect="207"
  $USE_AUTH || propfind_expect="207|401"

  dav_test "CalDAV PROPFIND calendars"   PROPFIND \
    "${BASE_URL}/remote.php/dav/calendars/${DAV_USER}/" "$propfind_expect"
  dav_test "CalDAV OPTIONS calendars"    OPTIONS  \
    "${BASE_URL}/remote.php/dav/calendars/"        "200" "DAV:"
  dav_test "CalDAV PROPFIND principals"  PROPFIND \
    "${BASE_URL}/remote.php/dav/principals/"       "$propfind_expect"
}

# ── CardDAV suite ──────────────────────────────────────────────────────────
run_carddav() {
  info "CardDAV endpoint probes (${BASE_URL})"
  local propfind_expect="207"
  $USE_AUTH || propfind_expect="207|401"

  dav_test "CardDAV PROPFIND addressbooks" PROPFIND \
    "${BASE_URL}/remote.php/dav/addressbooks/${DAV_USER}/" "$propfind_expect"
  dav_test "CardDAV OPTIONS addressbooks"  OPTIONS  \
    "${BASE_URL}/remote.php/dav/addressbooks/"        "200" "DAV:"
}

# ── run ────────────────────────────────────────────────────────────────────
info "OpenCloud DAV verification"
info "base-url : ${BASE_URL}"
info "user     : ${DAV_USER}"
if $USE_AUTH; then
  info "auth     : enabled (user=${AUTH_USER})"
else
  info "auth     : disabled (anonymous; 401 is acceptable for PROPFIND)"
fi

if ! command -v curl >/dev/null 2>&1; then
  err "curl is required but was not found on PATH"
  exit 1
fi

if $RUN_CALDAV; then
  run_caldav
fi
if $RUN_CARDDAV; then
  run_carddav
fi

echo
if [[ "$FAILS" -eq 0 ]]; then
  ok "All DAV endpoint probes passed."
  exit 0
else
  err "${FAILS} DAV probe(s) failed."
  exit 1
fi
