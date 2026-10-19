#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# openSME — Live Demo Deployer
# ═══════════════════════════════════════════════════════════════
# One-command deploy for home.opensme.graphwiz.ai.
# Starts: Portal, PostgreSQL, Redis, Memcached, Zitadel, OpenCloud.
# Skips:  PgBouncer, LDAP, Collabora, Stalwart, SOGo.
#
# Modes:
#   Normal:         Runs its own Traefik on ports 80/443.
#   Coexist:       Piggybacks on an existing Traefik (set COMPOSE_PROFILE=coexist).
#
# Prerequisites:
#   - Docker + Compose v2
#   - DNS: A records for home.opensme.graphwiz.ai, auth.home.opensme.graphwiz.ai,
#           cloud.home.opensme.graphwiz.ai → your server IP
#   - Ports 80 + 443 open and pointing to this host
#
# Usage:
#   ./scripts/demo-live.sh                # first run (creates .env)
#   ./scripts/demo-live.sh                # subsequent runs (reuses .env)
#   ./scripts/demo-live.sh --force-env    # regenerate .env with new passwords
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC}  $1"; }
step()  { echo -e "${CYAN}── ${1} ──${NC}"; }

# ── Prerequisites ──────────────────────────────────
step "Checking prerequisites"

if ! command -v docker &>/dev/null; then
  err "Docker not found. Install: https://docs.docker.com/engine/install/"
  exit 1
fi

if ! docker compose version &>/dev/null 2>&1; then
  err "Docker Compose v2 not found. Install: https://docs.docker.com/compose/install/"
  exit 1
fi

DOCKER_VERSION=$(docker --version | grep -oP '\d+\.\d+' | head -1)
COMPOSE_VERSION=$(docker compose version --short 2>/dev/null || echo "unknown")
ok "Docker ${DOCKER_VERSION}, Compose ${COMPOSE_VERSION}"

# ── Project root ───────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# ── Generate .env ───────────────────────────────────
FORCE_ENV=false
if [[ "${1:-}" == "--force-env" ]]; then
  FORCE_ENV=true
  warn "Regenerating .env (--force-env)"
fi

if [[ "$FORCE_ENV" == true ]] || [[ ! -f .env ]]; then
  step "Generating .env"

  # Prompt for ACME email
  echo ""
  echo -e "${CYAN}Enter your email for Let's Encrypt certificates:${NC}"
  echo -e "  (used for ACME registration and cert expiry alerts)"
  read -rp "  Email: " ACME_EMAIL
  if [[ -z "$ACME_EMAIL" ]]; then
    err "Email is required for Let's Encrypt."
    exit 1
  fi

  # Generate random secrets
  pw() { openssl rand -base64 24 | tr -d '/+=' | head -c 32; }

  # Generate Traefik dashboard password and hash
  TRAEFIK_PASS=$(openssl rand -base64 16)
  TRAEFIK_HASH=$(printf '%s' "$TRAEFIK_PASS" | openssl passwd -apr1 -stdin 2>/dev/null || printf 'admin:')
  # Compose interpolates $ inside .env values — escape $ → $$ so the
  # apr1 hash ($apr1$salt$hash) survives; compose unescapes it again
  # when injecting the variable into containers.
  TRAEFIK_HASH_ESC=${TRAEFIK_HASH//\$/\$\$}

  # Write to a temp file first, then move into place — a failed generation
  # must not leave a truncated .env that poisons subsequent runs
  # ("using existing .env" would then boot with compose defaults).
  env_tmp=$(mktemp)
  cat > "$env_tmp" <<ENVEOF
# openSME — Live Demo
# Generated: $(date -Iseconds)

# ── Domains ──
OPENSME_DOMAIN=home.opensme.graphwiz.ai
PORTAL_DOMAIN=home.opensme.graphwiz.ai
ZITADEL_DOMAIN=auth.home.opensme.graphwiz.ai
IDP_URL=https://"$ZITADEL_DOMAIN"
OPENCLOUD_DOMAIN=cloud.home.opensme.graphwiz.ai

# ── Database ──
POSTGRES_PASSWORD=$(pw)
ZITADEL_DB_PASSWORD=$(pw)
SOGO_DB_PASSWORD=$(pw)

# ── Zitadel ──
ZITADEL_ADMIN=admin
ZITADEL_ADMIN_PASSWORD=$(pw)

# ── OpenCloud ──
OC_ADMIN_USERNAME=admin
OC_ADMIN_PASSWORD=$(pw)
OC_OIDC_CLIENT_SECRET=$(pw)   # canonical key; opencloud.yml keeps OC_OIDC_SECRET as legacy alias

# ── Portal (empty = hidden from landing page) ──
MAIL_URL=
BPM_URL=
# INVOICE_URL=https://invoice.${OPENSME_DOMAIN}   # --profile invoice
# PAPERLESS_URL=https://docs.${OPENSME_DOMAIN}    # --profile paperless
# CHAT_URL=https://chat.${OPENSME_DOMAIN}         # --profile synapse
# NOTES_URL=https://notes.${OPENSME_DOMAIN}

# ── Traefik ──
TRAEFIK_ACME_EMAIL=${ACME_EMAIL}
# Let's Encrypt production CA (compose default is STAGING); keep this to get valid certs on the live demo
TRAEFIK_ACME_CA_SERVER=https://acme-v02.api.letsencrypt.org/directory
TRAEFIK_USERS=${TRAEFIK_HASH_ESC}

# ── Logging ──
LOG_LEVEL=info
LOG_PRETTY=true
ENVEOF

  mv "$env_tmp" .env

  ok ".env created (secrets auto-generated)"
else
  ok "Using existing .env"
fi

# ── Validate .env ─────────────────────────────────
step "Validating configuration"

# .env is generated at deploy time (not tracked)
# shellcheck source=/dev/null
source .env

DOMAINS=(
  "$PORTAL_DOMAIN"
  "$ZITADEL_DOMAIN"
  "$OPENCLOUD_DOMAIN"
)

MISSING=false
for d in "${DOMAINS[@]}"; do
  ip=$(dig +short "$d" 2>/dev/null | tail -1)
  if [[ -z "$ip" ]]; then
    warn "DNS: ${d} does not resolve yet"
    MISSING=true
  else
    ok "DNS: ${d} → ${ip}"
  fi
done

if [[ "$MISSING" == true ]]; then
  echo ""
  warn "Some DNS records are missing. The stack will start but HTTPS certs"
  warn "won't be issued until DNS is configured. Make sure these A records exist:"
  for d in "${DOMAINS[@]}"; do
    warn "  ${d} → <your-server-IP>"
  done
  echo ""
  read -rp "Continue anyway? [y/N] " CONT
  if [[ "$CONT" != [yY] ]]; then
    info "Aborted. Configure DNS and re-run."
    exit 0
  fi
fi

# ── Check ports ────────────────────────────────────
step "Checking ports"

for port in 80 443; do
  if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
    pid=$(ss -tlnp 2>/dev/null | grep ":${port} " | grep -oP 'pid=\K\d+' | head -1)
    warn "Port ${port} is in use (PID ${pid}). Traefik may fail to bind."
  else
    ok "Port ${port} available"
  fi
done

# ── Build and start ─────────────────────────────────
step "Building and starting openSME (live demo)"

info "Compose files:"
for f in docker-compose.yml idm/zitadel.yml opencloud/opencloud.yml profiles/demo.live.yml; do
  info "  ${f}"
done
echo ""

docker compose -f docker-compose.yml \
              -f idm/zitadel.yml \
              -f opencloud/opencloud.yml \
              -f profiles/demo.live.yml \
              up -d --build --remove-orphans

echo ""

# ── Wait for health ────────────────────────────────
step "Waiting for services to become healthy"

wait_for_healthy() {
  local name="$1" timeout="${2:-120}"
  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    status=$(docker inspect --format='{{.State.Health.Status}}' "$name" 2>/dev/null || echo "missing")
    if [[ "$status" == "healthy" ]]; then
      ok "$name is healthy"
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  warn "$name not healthy after ${timeout}s (status: ${status})"
  return 1
}

wait_for_healthy "opensme-postgres" 60
wait_for_healthy "opensme-redis"    30
wait_for_healthy "opensme-zitadel"  120 || true
wait_for_healthy "opensme-opencloud"  120 || true

# ── Summary ────────────────────────────────────────
ADMIN_PW=$(grep ZITADEL_ADMIN_PASSWORD .env | cut -d= -f2)
OC_ADMIN=$(grep OC_ADMIN_PASSWORD .env | cut -d= -f2)
TRAEFIK_PASS_DISPLAY="(see .env — TRAEFIK_USERS hash)"
if [[ -n "${TRAEFIK_PASS:-}" ]]; then
  TRAEFIK_PASS_DISPLAY="${TRAEFIK_PASS}"
fi

echo ""
echo -e "${GREEN}═════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  openSME — Live Demo is running!${NC}"
echo -e "${GREEN}═════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${CYAN}Services:${NC}"
echo -e "    Portal:     ${BLUE}https://${PORTAL_DOMAIN}${NC}"
echo -e "    Zitadel:   ${BLUE}https://${ZITADEL_DOMAIN}${NC}"
echo -e "    OpenCloud:  ${BLUE}https://${OPENCLOUD_DOMAIN}${NC}"
echo -e "    Traefik:    ${BLUE}https://traefik.${OPENSME_DOMAIN}${NC}"
echo ""
echo -e "  ${CYAN}Credentials:${NC}"
echo -e "    Zitadel admin:   ${YELLOW}admin / ${ADMIN_PW}${NC}"
echo -e "    OpenCloud admin: ${YELLOW}admin / ${OC_ADMIN}${NC}"
echo -e "    Traefik dashboard: ${YELLOW}admin / ${TRAEFIK_PASS_DISPLAY}${NC}"
echo ""
echo -e "  ${CYAN}Useful commands:${NC}"
echo -e "    Logs:   docker compose logs -f"
echo -e "    Stop:   docker compose down"
echo -e "    Status: docker compose ps"
echo ""
echo -e "${GREEN}───────────────────────────────────────────────────${NC}"
echo ""
