#!/usr/bin/env bash
# ── openSME — Generate a working .env from the template ──────
# Builds a functional .env with random secrets by transforming every
# `KEY=CHANGEME_*` placeholder in `.env.example` into a fresh value.
# Unlike a plain `cp .env.example .env`, this never ships the well-known
# CHANGEME passwords (a security footgun) and produces a stack that boots.
#
# Intentionally separate from scripts/demo.sh, which generates its own
# minimal .env AND boots the demo stack. init-env only *writes* the env so
# you can configure, review, then boot with `make up`.
#
# Usage:
#   ./scripts/init-env.sh             # write .env (refuses to clobber)
#   ./scripts/init-env.sh --force-env # regenerate .env
# ═══════════════════════════════════════════════

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

FORCE=false
if [[ "${1:-}" == "--force-env" ]]; then FORCE=true; fi

if [[ -f .env ]]; then
  if [[ "$FORCE" == false ]]; then
    warn ".env already exists — skipping (use --force-env to regenerate)."
    exit 0
  fi
  warn "Regenerating .env (--force-env)"
fi

if [[ ! -f .env.example ]]; then
  echo "[ERR] .env.example missing" >&2
  exit 1
fi

pw()    { openssl rand -base64 24; }
hex32() { openssl rand -hex 32; }
hex64() { openssl rand -hex 64; }

# Traefik dashboard basicauth wants 'user:<apr1>'; escape $ so compose does
# not try to interpolate the hash value.
TRAEFIK_PASS="$(openssl rand -base64 24)"
TRAEFIK_HASH="admin:$(printf '%s' "$TRAEFIK_PASS" | openssl passwd -apr1 -stdin)"
TRAEFIK_HASH_ESC="${TRAEFIK_HASH//\$/\$\$}"

tmp="$(mktemp)"
while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in
    'TRAEFIK_USERS='*)
      printf 'TRAEFIK_USERS=%s\n' "$TRAEFIK_HASH_ESC" >> "$tmp"
      ;;
    *'=CHANGEME_'*)
      key="${line%%=*}"
      val="${line#*=}"
      case "$val" in
        CHANGEME_generate_64_chars)            repl="$(hex64)";;
        CHANGEME_nosdesk_db|CHANGEME_nosdesk_jwt|CHANGEME_nosdesk_mfa) repl="$(hex32)";;
        CHANGEME_model_name)                   repl="llama3";;
        *)                                     repl="$(pw)";;
      esac
      printf '%s=%s\n' "$key" "$repl" >> "$tmp"
      ;;
    *)
      printf '%s\n' "$line" >> "$tmp"
      ;;
  esac
done < .env.example
mv "$tmp" .env
chmod 600 .env

# ── Self-check: nothing unresolved may ship ──
leftover=$(grep -c '=CHANGEME_' .env || true)
if [[ "$leftover" -ne 0 ]]; then
  warn "${leftover} unresolved CHANGEME_ placeholder(s) remain — review .env"
else
  ok "All CHANGEME_ placeholders replaced with random values"
fi

# ── Report ───────────────────────────────────
echo ""
ok ".env written (random secrets, locked 0600)"
info "Next:  make up PROFILE=soho        # or up-medium / up-all"
info "Full demo stack (one-command): ./scripts/demo.sh"
info "Diff your config against the template: diff <(grep -v '^#' .env) <(grep -v '^#' .env.example)"