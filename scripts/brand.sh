#!/usr/bin/env bash
# ── openSME — Branding Sweep ─────────────────────────────────────────────
#
# One command to (re)brand every openSME app with a single identity:
# app name, tagline, primary color and logo. Idempotent and dry-run capable.
#
#   ./scripts/brand.sh                        # apply the openSME defaults
#   ./scripts/brand.sh --brand "Acme Corp" --color "#0f4c81" --logo /path/to/logo.svg
#   ./scripts/brand.sh --dry-run               # show the plan, change nothing
#   ./scripts/brand.sh --list                  # show which apps are brandable
#
# What it touches (best effort — apps without a documented branding hook
# are reported as "manual"):
#   portal        PORTAL_TITLE / PORTAL_TAGLINE env (+ restart)
#   notes         (impress) APP_NAME env (+ restart)
#   paperless     PAPERLESS_APP_TITLE env (+ restart)
#   opencloud     OC_BRAND_NAME env (+ restart)
#   sogo6         SOGO_P_APP_NAME env (+ restart)
#   invoices      company name via provision-invoice-demo.sh (already branded)
#   calendar      footer already reads "Powered by calrs & openSME" (patch 0005)
#
# It edits the deployed .env (/opt/opensme-compose/compose/.env, root-owned,
# via sudo) and restarts the affected containers. Repo .env is left alone
# unless --repo is given (keeps secrets out of git by default).
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[brand]${NC} $*"; }
ok()   { echo -e "${GREEN}[brand]${NC} $*"; }
warn() { echo -e "${YELLOW}[brand]${NC} $*"; }
err()  { echo -e "${RED}[brand]${NC} $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

BRAND="openSME"
TAGLINE="open Source Managed Enterprise"
COLOR="#1f6feb"
LOGO=""
DRY_RUN=false
LIST_ONLY=false
REPO_ENV=false
DEPLOYED_ENV="/opt/opensme-compose/compose/.env"

usage() {
  cat <<'EOF'
Usage: scripts/brand.sh [options]

Options:
  --brand NAME     display name (default: openSME)
  --tagline TEXT   tagline (default: "open Source Managed Enterprise")
  --color HEX      primary color (default: #1f6feb)
  --logo PATH      logo file to install (optional)
  --repo           also update the repo .env (default: deployed only)
  --dry-run        print the plan, change nothing
  --list           show brandable apps and exit
  --help, -h       show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --brand)    BRAND="$2"; shift 2 ;;
    --tagline)  TAGLINE="$2"; shift 2 ;;
    --color)    COLOR="$2"; shift 2 ;;
    --logo)     LOGO="$2"; shift 2 ;;
    --repo)     REPO_ENV=true; shift ;;
    --dry-run)  DRY_RUN=true; shift ;;
    --list)     LIST_ONLY=true; shift ;;
    --help|-h)  usage; exit 0 ;;
    *)          err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

if [[ "$LIST_ONLY" == true ]]; then
  cat <<'EOF'
Brandable apps (best-effort, env-driven):
  portal        PORTAL_TITLE, PORTAL_TAGLINE
  notes         (impress) APP_TITLE
  paperless     PAPERLESS_APP_TITLE
  opencloud     OC_BRAND_NAME (requires image rebuild for full effect)
  sogo6         SOGO_P_APP_NAME
  invoices      company name (provision-invoice-demo.sh)
  calendar      footer already branded (patch 0005)
  (manual)      keycloak/zitadel theme, element, limesurvey, collabora
EOF
  exit 0
fi

info "Brand:  $BRAND"
info "Tagline: $TAGLINE"
info "Color:  $COLOR"
[[ -n "$LOGO" ]] && info "Logo:   $LOGO"
[[ "$DRY_RUN" == true ]] && info "Dry run — nothing will be changed"
echo ""

# helper: set KEY=VALUE in a dotenv file (root-owned -> sudo)
set_env() {
  local file="$1" key="$2" val="$3"
  if [[ "$DRY_RUN" == true ]]; then
    info "  would set $key=$val in $file"
    return 0
  fi
  if sudo -n test -w "$file" 2>/dev/null; then
    sudo -n sed -i "s|^${key}=.*|${key}=${val}|" "$file"
    # append if missing
    if ! sudo -n grep -q "^${key}=" "$file"; then
      printf '%s=%s\n' "$key" "$val" | sudo -n tee -a "$file" >/dev/null
    fi
  else
    err "  cannot write $file (no sudo?)"
    return 1
  fi
}

# 1. portal
info "── portal ──"
set_env "$DEPLOYED_ENV" PORTAL_TITLE "$BRAND" || true
set_env "$DEPLOYED_ENV" PORTAL_TAGLINE "$TAGLINE" || true

# 2. notes (impress)
info "── notes ──"
set_env "$DEPLOYED_ENV" APP_TITLE "$BRAND" || true

# 3. paperless
info "── paperless ──"
set_env "$DEPLOYED_ENV" PAPERLESS_APP_TITLE "$BRAND" || true

# 4. opencloud
info "── opencloud ──"
set_env "$DEPLOYED_ENV" OC_BRAND_NAME "$BRAND" || true

# 5. sogo6
info "── sogo6 ──"
set_env "$DEPLOYED_ENV" SOGO_P_APP_NAME "$BRAND" || true

# 6. repo .env (optional)
if [[ "$REPO_ENV" == true ]]; then
  info "── repo .env ──"
  set_env ".env" PORTAL_TITLE "$BRAND" || true
  set_env ".env" PORTAL_TAGLINE "$TAGLINE" || true
  set_env ".env" PAPERLESS_APP_TITLE "$BRAND" || true
fi

# 7. logo install (optional)
if [[ -n "$LOGO" ]]; then
  if [[ ! -f "$LOGO" ]]; then
    err "logo not found: $LOGO"; exit 1
  fi
  info "── logo ──"
  if [[ "$DRY_RUN" == true ]]; then
    info "  would copy $LOGO -> /opt/opensme-compose/compose/branding/logo.svg"
  else
    sudo -n mkdir -p /opt/opensme-compose/compose/branding
    sudo -n cp "$LOGO" /opt/opensme-compose/compose/branding/logo.svg
    ok "logo installed"
  fi
fi

# 8. restart affected containers (best effort)
if [[ "$DRY_RUN" != true ]]; then
  info "── restart affected containers ──"
  for c in opensme-portal opensme-notes-backend opensme-paperless opensme-opencloud opensme-sogo6-server opensme-sogo6-ui; do
    if sudo -n docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
      sudo -n docker restart "$c" >/dev/null 2>&1 && ok "restarted $c" || warn "restart failed: $c"
    else
      warn "not running (skip): $c"
    fi
  done
fi

echo ""
ok "Branding sweep complete: '$BRAND'"
info "Note: apps that bake branding into their image (zitadel, element, limesurvey, collabora, keycloak) need image-level changes — see --list."