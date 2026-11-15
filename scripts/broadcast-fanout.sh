#!/usr/bin/env bash
# scripts/broadcast-fanout.sh — Broadcast mail fanout via Stalwart SMTP
# ══════════════════════════════════════════════════════════════════════
# Sends one broadcast mail (BROADCAST_SUBJECT/BROADCAST_BODY) to all demo
# users through the Stalwart SMTP listener (default stalwart:25). This
# is the mail leg of the portal broadcast fanout: the Next.js route
# portal/web/src/app/api/broadcast/route.ts shells out to this script
# (no python3/SMTP stack inside the portal-web image; stalwart is
# reached by its compose-internal name, not 127.0.0.1).
#
# Modes:
#   broadcast-fanout.sh            one send pass (apply); requires
#                                  BROADCAST_BODY (exit 2 when missing)
#   broadcast-fanout.sh --dry-run  print subject/from/host/recipients;
#                                  no SMTP traffic; exits 0
#   broadcast-fanout.sh --check    recipients + TCP-connect audit,
#                                  read-only; exit != 0 on failed checks
#   --env-file PATH                env file to load (default .env in the
#                                  repo root); missing file is fine — the
#                                  defaults below are complete
#   --help, -h                     this help
#
# Environment (explicit environment beats the env file, docker semantics):
#   BROADCAST_SUBJECT    mail subject   (default '[openSME] Broadcast an alle')
#   BROADCAST_BODY       mail body text (default '' — apply fails without it)
#   BROADCAST_MAIL_TO    comma-separated recipient list; WINS over JSON
#                        discovery when non-empty
#   BROADCAST_MAIL_DOMAIN optional re-domaining for discovered recipients
#                        (default: top-level 'domain' of
#                        bootstrap/demo-users.json → opensme.local)
#   BROADCAST_MAIL_FROM  sender address (default broadcast@$BROADCAST_MAIL_DOMAIN)
#   BROADCAST_SMTP_HOST  SMTP host      (default 127.0.0.1 — stalwart)
#   BROADCAST_SMTP_PORT  SMTP port      (default 25; 587 uses STARTTLS with
#                        an unverified TLS context — demo stack has a
#                        self-signed cert; no SMTP auth is attempted)
#
# Recipient discovery mirrors scripts/provision-demo-users.sh: the users
# of bootstrap/demo-users.json (13 demo users + testuser), local parts
# re-domained onto $BROADCAST_MAIL_DOMAIN when that is set, deduplicated.
# Delivery is per-recipient and best-effort: Stalwart principals
# auto-provision on the user's FIRST SSO login, so a fresh stack delivers
# only to the mailboxes that exist (testuser out of the box) and prints
# the missing ones — that is a provisioning state, not a send failure.
#
# Secrets discipline: this script handles no secrets (port 25 on the
# compose-internal interface is unauthenticated by design, 587 STARTTLS
# without auth). `set +x` forbids tracing anyway and the python3 payload
# travels via heredoc stdin, never as argv.
#
# Exit codes: 0 success/dry-run, 1 runtime failure, 2 usage error.
# Requires: jq (recipient discovery when BROADCAST_MAIL_TO is unset),
# python3 (SMTP send / TCP check).
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail
set +x   # never trace: env values flow through heredocs below

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC}  $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1

usage() {
  sed -n '2,48p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

MODE="apply"          # apply | dry-run | check
ENV_FILE="$ROOT/.env"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)  MODE="dry-run"; shift ;;
    --check)    MODE="check"; shift ;;
    --env-file) [[ -n "${2:-}" ]] || { err "--env-file requires a value"; exit 2; }
                ENV_FILE="$2"; shift 2 ;;
    --help|-h)  usage; exit 0 ;;
    *)          err "Unknown option: $1 (see --help)"; exit 2 ;;
  esac
done

# ── env-quote hardening (incident: 'Source: command not found') ────────────
# Same hardened dotenv loader as booking-notify.sh / calendar-setup.sh /
# demo-data.sh (the block between the markers below is byte-identical
# across the scripts; tests/00-static/check_env_quote.py pins the
# pattern). Every dotenv load scrubs unquoted multi-word values FIRST:
# offending lines are rewritten double-quoted in place (with a .bak-<date>
# backup and a loud warning naming file+line) before the `source`, and the
# loader can never die on such a line. Idempotent: quoted lines never match.
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
ENV_KEYS=(BROADCAST_SUBJECT BROADCAST_BODY BROADCAST_MAIL_TO
          BROADCAST_MAIL_DOMAIN BROADCAST_MAIL_FROM
          BROADCAST_SMTP_HOST BROADCAST_SMTP_PORT)
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
# stalwart is reachable on opensme-net (the portal-web container shares its
# netns with the Rust portal, NOT with stalwart — so 127.0.0.1 is wrong).
BROADCAST_SMTP_HOST="${BROADCAST_SMTP_HOST:-stalwart}"
BROADCAST_SMTP_PORT="${BROADCAST_SMTP_PORT:-25}"
BROADCAST_SUBJECT="${BROADCAST_SUBJECT:-[openSME] Broadcast an alle}"
BROADCAST_BODY="${BROADCAST_BODY:-}"
USERS_JSON="$ROOT/bootstrap/demo-users.json"

# mail domain: explicit override wins, else the JSON top-level domain,
# else the documented demo default.
MAIL_DOMAIN="${BROADCAST_MAIL_DOMAIN:-}"
if [[ -z "$MAIL_DOMAIN" && -f "$USERS_JSON" ]] && command -v jq >/dev/null 2>&1; then
  MAIL_DOMAIN="$(jq -r '(.domain // "opensme.local")' "$USERS_JSON" 2>/dev/null)" || MAIL_DOMAIN=""
  [[ -z "$MAIL_DOMAIN" || "$MAIL_DOMAIN" == "null" ]] && MAIL_DOMAIN="opensme.local"
fi
MAIL_DOMAIN="${MAIL_DOMAIN:-opensme.local}"
BROADCAST_MAIL_FROM="${BROADCAST_MAIL_FROM:-broadcast@${MAIL_DOMAIN}}"

# ── recipients: BROADCAST_MAIL_TO wins, else bootstrap/demo-users.json ──────
recipients_from_json() {
  # local parts re-domained onto $MAIL_DOMAIN, deduplicated, order kept
  jq -r --arg dom "$MAIL_DOMAIN" '
    (if type == "object" then (.users // [ .[] | select(type == "object") ])
     else . end)[]
    | ((.email // .username // empty) | split("@")[0]) | select(length > 0)
    | (. + "@" + $dom)
  ' "$USERS_JSON" 2>/dev/null | awk 'NF' | awk '!seen[$0]++'
}

RECIPIENTS=""
RECIPIENT_SOURCE=""
if [[ -n "${BROADCAST_MAIL_TO:-}" ]]; then
  RECIPIENTS="$(printf '%s' "$BROADCAST_MAIL_TO" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' || true)"
  RECIPIENT_SOURCE="BROADCAST_MAIL_TO"
else
  if ! command -v jq >/dev/null 2>&1; then
    err "jq not found and BROADCAST_MAIL_TO unset — cannot discover recipients"
    exit 1
  fi
  [[ -f "$USERS_JSON" ]] || { err "recipient source missing: ${USERS_JSON#$ROOT/} (set BROADCAST_MAIL_TO)"; exit 1; }
  RECIPIENTS="$(recipients_from_json || true)"
  RECIPIENT_SOURCE="${USERS_JSON#$ROOT/}"
fi
RECIPIENTS="$(printf '%s\n' "$RECIPIENTS" | awk 'NF' | awk '!seen[$0]++')"
RECIPIENT_COUNT="$(printf '%s\n' "$RECIPIENTS" | grep -c . || true)"
RECIPIENT_LIST="$(printf '%s' "$RECIPIENTS" | paste -sd, -)"

# ── SMTP sender (python3 stdlib only) ──────────────────────────────────────
smtp_send() {
  BROADCAST_MAIL_FROM="$BROADCAST_MAIL_FROM" \
  BROADCAST_SUBJECT="$BROADCAST_SUBJECT" \
  BROADCAST_BODY="$BROADCAST_BODY" \
  BROADCAST_MAIL_TO="$RECIPIENT_LIST" \
  BROADCAST_SMTP_HOST="$BROADCAST_SMTP_HOST" \
  BROADCAST_SMTP_PORT="$BROADCAST_SMTP_PORT" \
  python3 - <<'PY'
import os, smtplib, ssl, sys
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

host = os.environ["BROADCAST_SMTP_HOST"]
port = int(os.environ["BROADCAST_SMTP_PORT"])
from_addr = os.environ["BROADCAST_MAIL_FROM"]
subject = os.environ["BROADCAST_SUBJECT"]
body = os.environ["BROADCAST_BODY"]
recips = [r for r in (x.strip() for x in os.environ["BROADCAST_MAIL_TO"].split(",")) if r]
if not recips:
    print("no recipients", file=sys.stderr)
    sys.exit(1)

# Per-recipient transactions, best-effort: demo mailboxes only exist after
# the user's first SSO login (Stalwart principals auto-provision), so a
# fresh stack delivers to a subset and the rest are REPORTED, not fatal.
# One connection per recipient also sidesteps Stalwart dropping a session
# after a burst of unknown-recipient RCPTs.
ctx = None
if port == 587:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # demo stack: self-signed cert

delivered, missing = [], []
send_lock = __import__("threading").Lock()

def deliver(rcpt):
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = rcpt
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.ehlo()
            if port == 587:
                s.starttls(context=ctx)
                s.ehlo()
            s.send_message(msg)
        with send_lock:
            delivered.append(rcpt)
    except Exception as exc:  # noqa: BLE001 — one line per missing recipient
        with send_lock:
            missing.append(f"{rcpt} ({exc})")

# 5 workers: 14 demo users ≈ 3 batches — keeps the route's execFile budget
# small even when the listener is slow to greet (measured ~4.5 s/connection).
with ThreadPoolExecutor(max_workers=5) as pool:
    list(pool.map(deliver, recips))

print(f"delivered {len(delivered)}/{len(recips)} recipient(s)")
for m in missing:
    print(f"missing: {m}", file=sys.stderr)
sys.exit(0 if delivered else 1)
PY
}

# ── TCP reachability probe for --check ─────────────────────────────────────
smtp_probe() {
  BROADCAST_SMTP_HOST="$BROADCAST_SMTP_HOST" \
  BROADCAST_SMTP_PORT="$BROADCAST_SMTP_PORT" \
  python3 - <<'PY'
import os, smtplib, sys
host = os.environ["BROADCAST_SMTP_HOST"]
port = int(os.environ["BROADCAST_SMTP_PORT"])
try:
    with smtplib.SMTP(host, port, timeout=10) as s:
        code, _ = s.noop()
        print(f"connected, NOOP -> {code}")
except Exception as exc:  # noqa: BLE001
    print(f"probe failure: {exc}", file=sys.stderr)
    sys.exit(1)
PY
}

case "$MODE" in
  dry-run)
    info "dry-run — no SMTP traffic"
    info "subject:    $BROADCAST_SUBJECT"
    info "from:       $BROADCAST_MAIL_FROM"
    info "smtp:       $BROADCAST_SMTP_HOST:$BROADCAST_SMTP_PORT"
    info "recipients: $RECIPIENT_COUNT (source: $RECIPIENT_SOURCE)"
    printf '%s\n' "$RECIPIENTS" | sed 's/^/  - /'
    ok "dry-run complete"
    exit 0
    ;;
  check)
    fails=0
    if command -v python3 >/dev/null 2>&1; then
      ok "python3 present: $(command -v python3)"
    else
      err "python3 not found — SMTP send impossible"
      fails=$((fails + 1))
    fi
    if [[ "$RECIPIENT_COUNT" -gt 0 ]]; then
      ok "recipients: $RECIPIENT_COUNT (source: $RECIPIENT_SOURCE)"
    else
      err "no recipients resolved (set BROADCAST_MAIL_TO or fix ${USERS_JSON#$ROOT/})"
      fails=$((fails + 1))
    fi
    if smtp_probe; then
      ok "SMTP reachable at $BROADCAST_SMTP_HOST:$BROADCAST_SMTP_PORT"
    else
      err "SMTP NOT reachable at $BROADCAST_SMTP_HOST:$BROADCAST_SMTP_PORT"
      fails=$((fails + 1))
    fi
    if [[ "$fails" -gt 0 ]]; then
      err "check FAILED ($fails problem(s))"
      exit 1
    fi
    ok "check PASSED"
    exit 0
    ;;
  apply)
    [[ -n "$BROADCAST_BODY" ]] || { err "BROADCAST_BODY is empty — nothing to broadcast (usage error)"; exit 2; }
    command -v python3 >/dev/null 2>&1 || { err "Required tool not found: python3"; exit 1; }
    [[ "$RECIPIENT_COUNT" -gt 0 ]] || { err "no recipients resolved — refusing to send"; exit 1; }
    info "broadcast via $BROADCAST_SMTP_HOST:$BROADCAST_SMTP_PORT from $BROADCAST_MAIL_FROM to $RECIPIENT_COUNT recipient(s)"
    if SEND_OUT="$(smtp_send)"; then
      ok "broadcast: ${SEND_OUT:-delivered}"
      exit 0
    fi
    err "broadcast send failed — nothing delivered"
    exit 1
    ;;
esac
