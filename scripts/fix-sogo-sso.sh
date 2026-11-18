#!/usr/bin/env bash
# ── openSME — Repair SOGo6 SSO allow-redirect list ─────────────────────
#
# The SOGo6 default domain settings shipped with an allow-redirect list
# pointing at opensme.graphwiz.ai (missing the 'home.' prefix used by this
# deployment), so the OIDC callback URI the server emits was not in the
# allow list. The callback's {tenant} suffix is derived from the USER's
# login domain, so BOTH the webmail host's parent (SOGO_OIDC_DOMAIN) and the
# IdP instance login domain (ZITADEL_LOGIN_DOMAIN, default
# opensme.${ZITADEL_DOMAIN}) must be listed — 2026-11-18 outage:
# .../callback/opensme.auth.home.opensme.graphwiz.ai was missing here and in
# the Zitadel client, so the IdP answered authorize with 400 invalid_request.
# Idempotent: re-running just re-applies the correct list.
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

# Safe .env load: only plain KEY=value lines without spaces/quotes (the live
# .env carries e.g. OAUTH_SCOPES=openid email profile which is not shell-safe).
if [[ -f .env ]]; then
  while IFS= read -r l; do
    [[ "$l" =~ ^[A-Za-z_][A-Za-z0-9_]*=[^\"[:space:]]*$ ]] && export "$l"
  done < .env
fi

if ! sudo -n docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$PG"; then
  err "$PG not running — cannot repair"; exit 1
fi

DOMAIN_A="${OPENSME_DOMAIN:-opensme.graphwiz.ai}"
ZITADEL_D="${ZITADEL_DOMAIN:-auth.${DOMAIN_A}}"
WEB_HOST="${SOGO_DOMAIN:-webmail.${DOMAIN_A}}"
WEB_URL="${SOGO6_PUBLIC_URL:-https://${WEB_HOST}}"
WEB_HOST_URL="${WEB_URL#https://}"; WEB_HOST_URL="${WEB_HOST_URL#http://}"
TENANT_WEB="${SOGO_OIDC_DOMAIN:-${WEB_HOST#webmail.}}"
TENANT_IDP="${ZITADEL_LOGIN_DOMAIN:-opensme.${ZITADEL_D}}"
DESIRED="$(python3 -c '
import json, sys
host, tenants = sys.argv[1], sys.argv[2:]
uris = []
for t in dict.fromkeys(tenants):
    uris += [f"https://{host}/api/user/v1/auth/callback/{t}",
             f"http://{host}/api/user/v1/auth/callback/{t}"]
print(json.dumps(uris))
' "$WEB_HOST_URL" "$TENANT_WEB" "$TENANT_IDP")"

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