#!/usr/bin/env bash
# ── openSME — Demo Launcher ─────────────
# One-command setup for demo / local development.
# Requires: Docker + Docker Compose (v2).
# Starts: Portal, PostgreSQL, Redis, Memcached, Zitadel, OpenCloud.
# Skips: PgBouncer, Collabora, Stalwart, SOGo (too heavy for demo).
#
# Usage:
#   ./scripts/demo.sh                    # first run (creates .env)
#   ./scripts/demo.sh --force-env        # regenerate .env with new passwords
#
# Services accessible at http://localhost:8080 (Portal only).
# Zitadel and OpenCloud are routed through Traefik on localhost.
# For a public demo with real HTTPS, see scripts/demo-live.sh.
# ═══════════════════════════════════════════════

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC}  $1"; }

# ── Prerequisites ─────────────────────────────
info "Checking prerequisites..."

if ! command -v docker &>/dev/null; then
  err "Docker not found. Install Docker first: https://docs.docker.com/engine/install/"
  exit 1
fi

if ! docker compose version &>/dev/null; then
  err "Docker Compose v2 not found. Install: https://docs.docker.com/compose/install/"
  exit 1
fi

DOCKER_VERSION=$(docker --version | grep -oP '\d+\.\d+' | head -1)
COMPOSE_VERSION=$(docker compose version --short 2>/dev/null || echo "unknown")
info "Docker ${DOCKER_VERSION}, Compose ${COMPOSE_VERSION}"

# ── Check RAM ─────────────────────────────────
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo "0")
TOTAL_RAM_GB=$((TOTAL_RAM_KB / 1024 / 1024))
if [ "$TOTAL_RAM_GB" -gt 0 ] && [ "$TOTAL_RAM_GB" -lt 4 ]; then
  warn "Only ${TOTAL_RAM_GB} GB RAM detected. 4 GB minimum recommended."
  warn "The demo may run slowly or fail."
fi

# ── Determine project root ────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# ── Generate .env if not exists ───────────────
FORCE_ENV=false
if [[ "${1:-}" == "--force-env" ]]; then
  FORCE_ENV=true
  warn "Regenerating .env (--force-env)"
fi

# Random password generator (needed by the generation block below AND the
# upgrade-append block — must be defined unconditionally)
pw() { openssl rand -base64 24; }

if [[ "$FORCE_ENV" == true ]] || [[ ! -f .env ]]; then
  info "Generating .env with random passwords..."

  # Generate Traefik dashboard password and hash — Traefik basicauth
  # requires 'user:hash'; openssl emits the bare hash.
  TRAEFIK_PASS=$(pw)
  TRAEFIK_HASH="admin:$(printf '%s' "$TRAEFIK_PASS" | openssl passwd -apr1 -stdin 2>/dev/null || true)"
  # Compose interpolates $ inside .env values — escape $ → $$ so the
  # apr1 hash ($apr1$salt$hash) survives; compose unescapes it again
  # when injecting the variable into containers.
  TRAEFIK_HASH_ESC=${TRAEFIK_HASH//\$/\$\$}

  # Write to a temp file first, then move into place — a failed generation
  # must not leave a truncated .env that poisons subsequent runs
  # ("using existing .env" would then boot with compose defaults).
  env_tmp=$(mktemp)
  cat > "$env_tmp" <<ENVEOF
# openSME — Local Demo Configuration
OPENSME_DOMAIN=opensme.local
OPENCLOUD_DOMAIN=cloud.opensme.local
ZITADEL_DOMAIN=auth.opensme.local
IDP_URL=https://auth.opensme.local
PORTAL_DOMAIN=portal.opensme.local
MAIL_DOMAIN=mail.opensme.local
SOGO_DOMAIN=webmail.opensme.local
COLLABORA_DOMAIN=collabora.opensme.local

# Random passwords
POSTGRES_PASSWORD=$(pw)
REDIS_PASSWORD=$(pw)
ZITADEL_DB_PASSWORD=$(pw)
SOGO_DB_PASSWORD=$(pw)
LDAP_ADMIN_PASSWORD=$(pw)
LDAP_USER_PASSWORD=$(pw)
ZITADEL_ADMIN_PASSWORD=$(pw)
OC_ADMIN_PASSWORD=$(pw)
OC_OIDC_CLIENT_SECRET=$(pw)
OC_S3_SECRET_KEY=$(pw)
COLLABORA_PASSWORD=$(pw)
NOSDESK_DB_PASSWORD=$(pw)
NOSDESK_JWT_SECRET=$(pw)
NOSDESK_MFA_KEK=$(openssl rand -hex 32)

# Traefik dashboard
TRAEFIK_USERS=${TRAEFIK_HASH_ESC}

LOG_LEVEL=debug
LOG_PRETTY=true
ENVEOF
  mv "$env_tmp" .env
  ok ".env created with random passwords"
else
  info "Using existing .env"
fi

# ── Keep .env complete across upgrades ────────
# New required variables introduced after an .env was generated are
# appended with a fresh random value instead of failing the boot.
if ! grep -q '^REDIS_PASSWORD=' .env; then
  printf '\nREDIS_PASSWORD=%s\n' "$(pw)" >> .env
  ok "Appended REDIS_PASSWORD to existing .env (new required var)"
fi
# hex, not base64: NOSDESK_DB_PASSWORD is embedded in a postgres:// URL —
# base64 '/' and '+' would break URL parsing
if ! grep -q '^NOSDESK_DB_PASSWORD=' .env; then
  printf 'NOSDESK_DB_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> .env
  ok "Appended NOSDESK_DB_PASSWORD to existing .env (new required var)"
fi
if ! grep -q '^NOSDESK_JWT_SECRET=' .env; then
  printf 'NOSDESK_JWT_SECRET=%s\n' "$(pw)" >> .env
  ok "Appended NOSDESK_JWT_SECRET to existing .env (new required var)"
fi
if ! grep -q '^NOSDESK_MFA_KEK=' .env; then
  printf 'NOSDESK_MFA_KEK=%s\n' "$(openssl rand -hex 32)" >> .env
  ok "Appended NOSDESK_MFA_KEK to existing .env (new required var)"
fi
# Older .env files stored a bare apr1 hash; Traefik basicauth needs
# 'user:hash' — regenerate so the dashboard is actually usable.
# OpenCloud <-> Zitadel OIDC client secret. scripts/zitadel-client.sh replaces
# this with the Zitadel-issued secret on the first boot that registers the
# persistent OIDC client; keeping it present prevents the CHANGEME fallback.
if ! grep -q '^OC_OIDC_CLIENT_SECRET=' .env; then
  printf '\nOC_OIDC_CLIENT_SECRET=%s\n' "$(pw)" >> .env
  ok "Appended OC_OIDC_CLIENT_SECRET to existing .env (new required var)"
fi

TU_VAL=$(grep -m1 '^TRAEFIK_USERS=' .env | cut -d= -f2-)
if [[ "$TU_VAL" != *:* ]]; then
  TRAEFIK_PASS=$(pw)
  HASH=$(printf '%s' "$TRAEFIK_PASS" | openssl passwd -apr1 -stdin | sed 's/\$/\$\$/g')
  awk -v line="TRAEFIK_USERS=admin:${HASH}" '/^TRAEFIK_USERS=/{print line; next} {print}' .env > .env.tmp \
    && mv .env.tmp .env
  ok "Regenerated TRAEFIK_USERS with user:hash prefix (dashboard auth fixed)"
fi

# Provision deploy-time Zitadel artifacts via apt (no Docker Hub pulls).
# Both are gitignored and normally created by the ansible deploy role.
# The masterkey must be exactly 32 bytes with no trailing newline.
if [[ ! -s idm/secrets/masterkey ]]; then
  mkdir -p idm/secrets
  printf '%s' "$(openssl rand -base64 24)" > idm/secrets/masterkey
  ok "Generated idm/secrets/masterkey"
fi
if [[ ! -x idm/zitadel/busybox ]]; then
  mkdir -p idm/zitadel
  if ! command -v busybox >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq busybox-static
  fi
  cp "$(which busybox)" idm/zitadel/busybox
  chmod +x idm/zitadel/busybox
  ok "Extracted busybox for the Zitadel healthcheck"
fi
# The healthcheck runs inside the distroless (static) Zitadel image which has
# NO dynamic loader — a dynamically linked busybox fails exec with ENOENT and
# the container is marked unhealthy (Traefik then skips it). Re-extract if we
# ever picked up a non-static binary.
if ! file idm/zitadel/busybox 2>/dev/null | grep -q 'static'; then
  sudo apt-get update -qq && sudo apt-get install -y -qq busybox-static
  cp "$(which busybox)" idm/zitadel/busybox
  chmod +x idm/zitadel/busybox
  ok "Re-extracted static busybox for the Zitadel healthcheck"
fi

# ── Initialize Zitadel (fresh installs) ─────
# The service command is plain `start`; on a fresh database it needs the
# one-time `init` + `setup` (idempotent — safe on every run) which also
# seeds the automation machine user and writes its PATs into the
# zitadel-machinekey volume (used by tests/05-e2e).
# Array (not string): each -f must stay its own argv element — quoting a
# flat string would pass the whole flag list as ONE argument.
CF=(-f docker-compose.yml -f idm/zitadel.yml -f opencloud/opencloud.yml -f profiles/demo.dev.yml --profile standalone)
info "Waiting for PostgreSQL..."
docker compose "${CF[@]}" up -d postgres
for _ in $(seq 1 60); do
  docker compose "${CF[@]}" ps --format json postgres 2>/dev/null | grep -qi '"health":"healthy"' && break
  sleep 5
done
info "Running Zitadel init + setup (idempotent)..."
docker compose "${CF[@]}" run --rm zitadel init
# --user 0:0: a freshly created named volume is root-owned and the
# distroless image runs as uid 1000 — without this the PAT files cannot
# be created on a first-ever run. Files land 0644 (world-readable).
if ! docker compose "${CF[@]}" run --rm --user 0:0 zitadel setup --masterkeyFile /secrets/masterkey --steps /steps.yaml; then
  err "Zitadel setup failed — check the logs above"
  exit 1
fi

# ── Register the persistent OpenCloud OIDC client (idempotent) ──
# OpenCloud logs in through Zitadel; without a registered OIDC client the web
# login is a dead end. The IdP (and the Traefik edge it sits behind) is started
# BEFORE the rest of the stack so the registration finishes before OpenCloud's
# first boot: the script writes the Zitadel-issued client id + secret into .env,
# which the full `up` below then passes to the opencloud container. Idempotent
# and cheap: on revisits the script reports "already registered" and exits 0.
info "Starting the IdP (Zitadel) ahead of the rest of the stack..."
docker compose "${CF[@]}" up -d traefik zitadel
IDP_READY="no"
for _ in $(seq 1 60); do
  # container-health == the /debug/ready healthcheck passing inside the image
  st=$(docker compose "${CF[@]}" ps --format json zitadel 2>/dev/null || true)
  if printf '%s' "$st" | grep -qi '"health":"healthy"'; then
    IDP_READY="yes"; break
  fi
  # bail early if the container died (don't spin the full 5 minutes)
  if printf '%s' "$st" | grep -qEi '"state":"(exited|dead|removed)"'; then
    break
  fi
  sleep 5
done
if [[ "$IDP_READY" == "yes" ]]; then
  info "Registering the persistent OpenCloud OIDC client in Zitadel (idempotent)..."
  if ! "${SCRIPT_DIR}/zitadel-client.sh"; then
    warn "OIDC client registration failed — see the message above (the e2e suite degrades to a warning)"
  fi
else
  warn "Zitadel did not become healthy — skipping OIDC client registration"
  warn "(web login against the IdP would fail; the e2e suite degrades to a warning)"
fi

# ── Build and start ──────────────────────────
# File order matters: overlays first, then demo profile (must be last to win).
info "Building and starting openSME (demo mode)..."

# Build and start — locals always rebuild so source edits land in the image.
docker compose \
  -f docker-compose.yml \
  -f idm/zitadel.yml \
  -f opencloud/opencloud.yml \
  -f profiles/demo.dev.yml \
  --profile standalone \
  up -d --build

# Traefik was started early (above) so the IdP edge exists for the OIDC
# client registration, but the Docker provider does not always pick up
# containers that join the network AFTER traefik itself (portal/opencloud
# -> 404 on their https routes until traefik re-evaluates). Force one
# clean reload so every late-starting service is routed.
docker compose \
  -f docker-compose.yml \
  -f idm/zitadel.yml \
  -f opencloud/opencloud.yml \
  -f profiles/demo.dev.yml \
  --profile standalone \
  restart traefik

echo ""
ok "openSME Demo is running!"

# ── Verify the stack actually came up ────────
# A one-command bootstrap should not print "running" while the Portal is
# still unreachable. The wait is bounded and never fails the boot: a slow
# or absent container reports status instead of aborting.
info "Waiting for the Portal to be reachable..."
PORTAL_READY="no"
for _ in $(seq 1 45); do
  health=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
    http://localhost:8080/health 2>/dev/null || echo 000)
  [[ "$health" == "200" ]] && { PORTAL_READY="yes"; break; }
  sleep 5
done
if [[ "$PORTAL_READY" == "yes" ]]; then
  ok "Portal is up (http://localhost:8080/health)"
else
  warn "Portal did not answer on http://localhost:8080/health yet."
  warn "Check the logs: docker compose ${CF[*]} logs portal"
fi

echo ""
info "Service health overview:"
for c in opensme-portal opensme-zitadel opensme-opencloud \
         opensme-postgres opensme-redis opensme-memcached opensme-traefik; do
  st=$(docker inspect --format '{{.State.Health.Status}}' "$c" 2>/dev/null || echo "absent")
  case "$st" in
    healthy)  echo -e "  ${GREEN}✓${NC} $c (healthy)";;
    *)        echo -e "  ${YELLOW}…${NC} $c ($st)";;
  esac
done

# ── Get admin password ────────────────────────
ADMIN_PW=$(grep ZITADEL_ADMIN_PASSWORD .env | cut -d= -f2)
OC_ADMIN=$(grep OC_ADMIN_PASSWORD .env | cut -d= -f2)
TRAEFIK_HASH=$(grep TRAEFIK_USERS .env | cut -d= -f2-)
TRAEFIK_PASS_DISPLAY="(see .env — TRAEFIK_USERS hash)"
if [[ -n "${TRAEFIK_PASS:-}" ]]; then
  TRAEFIK_PASS_DISPLAY="${TRAEFIK_PASS}"
fi

echo ""
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo -e "${GREEN}  openSME Demo — Credentials${NC}"
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo ""
echo -e "  Portal:       ${BLUE}http://localhost:8080${NC}"
echo ""
echo -e "  ${YELLOW}Zitadel Admin:${NC}"
echo -e "    User:     admin"
echo -e "    Password: ${ADMIN_PW}"
echo ""
echo -e "  ${YELLOW}OpenCloud Admin:${NC}"
echo -e "    User:     admin"
echo -e "    Password: ${OC_ADMIN}"
echo ""
echo -e "  ${YELLOW}Traefik Dashboard (if enabled):${NC}"
echo -e "    User:     admin"
echo -e "    Password: ${TRAEFIK_PASS_DISPLAY}"
echo ""
echo -e "${GREEN}───────────────────────────────────────────${NC}"
echo ""
info "To stop:   docker compose -f docker-compose.yml -f idm/zitadel.yml -f opencloud/opencloud.yml -f profiles/demo.dev.yml --profile standalone down"
info "To follow: docker compose logs -f"
info "For public HTTPS demo, see scripts/demo-live.sh"
echo ""
# ── Seed next steps (zero-friction hints) ────
# Bootstrap is two more deliberate steps away from a fully packed demo:
#  1. Workflow processes (BPMN) — only when the engine is enabled.
#  2. Sample documents — human-readable content, never run in CI.
if docker ps --format '{{.Names}}' | grep -qi 'opensme-camunda\|opensme-operaton'; then
  info "Workflow engine detected — deploy the sample processes:"
  info "    make bpm-deploy   # or: ./bootstrap/bpmn-deploy.sh"
fi
info "Sample documents (loading guide + files): bootstrap/seed-content/"
info "    see bootstrap/seed-content/README.md — load into Cloud / Paperless / Team Notes"
