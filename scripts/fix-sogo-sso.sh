#!/usr/bin/env bash
# ── openSME — Repair SOGo6 SSO allow-redirect list ─────────────────────
#
# The SOGo6 default domain settings shipped with an allow-redirect list
# pointing at opensme.graphwiz.ai (missing the 'home.' prefix used by this
# deployment), so the OIDC callback URI the server emits
# (/api/user/v1/auth/callback/home.opensme.graphwiz.ai) was not in the
# allow list. Idempotent: re-running just re-applies the correct list.
#
# Usage:
#   ./scripts/fix-sogo-sso.sh           # apply
#   ./scripts/fix-sogo-sso.sh --dry-run # show current vs desired
#   ./scripts/fix-sogo-sso.sh --check   # exit 0 only if already correct
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[sogo-sso]${NC} $*"; }
ok()   { echo -e "${GREEN}[sogo-sso]${NC} $*"; }
warn() { echo -e "${YELLOW}[sogo-sso]${NC} $*"; }
err()  { echo -e "${RED}[sogo-sso]${NC} $*" >&2; }

MODE="apply"
DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --check)   MODE="check"; shift ;;
    --help|-h) sed -n '2,16p' "$0"; exit 0 ;;
    *) err "Unknown option: $1"; exit 1 ;;
  esac
done

PG="opensme-sogo6-postgres"
DB="sogo"
USER="sogo"

if ! sudo -n docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$PG"; then
  err "$PG not running — cannot repair"; exit 1
fi

DESIRED='["https://webmail.home.opensme.graphwiz.ai/api/user/v1/auth/callback/home.opensme.graphwiz.ai", "http://webmail.home.opensme.graphwiz.ai/api/user/v1/auth/callback/home.opensme.graphwiz.ai"]'

CURRENT="$(sudo -n docker exec "$PG" psql -U "$USER" -d "$DB" -At -c \
  "SELECT settings_domain_default->'AUTH_SETTINGS'->'SOGO_D_OPENID_ALLOW_REDIRECT' FROM sogo6_sogo_settings;" 2>/dev/null | tr -d '[:space:]')"

info "current: $CURRENT"
info "desired: $DESIRED"

CURRENT_NORM="$(echo "$CURRENT" | tr -d ' ')"
DESIRED_NORM="$(echo "$DESIRED" | tr -d ' ')"

if [[ "$CURRENT_NORM" == "$DESIRED_NORM" ]]; then
  ok "allow-redirect list already correct"
  [[ "$MODE" == "check" ]] && exit 0
  exit 0
fi

if [[ "$MODE" == "check" ]]; then
  err "allow-redirect list is stale (see above)"; exit 1
fi

if [[ "$DRY_RUN" == true ]]; then
  info "dry-run — would UPDATE sogo6_sogo_settings"
  exit 0
fi

sudo -n docker exec "$PG" psql -U "$USER" -d "$DB" -c \
  "UPDATE sogo6_sogo_settings SET settings_domain_default = jsonb_set(settings_domain_default, '{AUTH_SETTINGS,SOGO_D_OPENID_ALLOW_REDIRECT}', '${DESIRED}'::jsonb) WHERE settings_unique = 1;" >/dev/null

ok "allow-redirect list repaired (restart sogo6-server to pick it up: docker restart opensme-sogo6-server)"