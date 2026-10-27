#!/usr/bin/env bash
# ── openSME — Bootstrap (all services) ─────────────
# One-command greenfield bootstrap of the FULL openSME suite:
#
#   1. generate .env from the template (random secrets; refuses to clobber)
#   2. bring up EVERY service — every tier + every optional profile
#      (make up-all: paperless, tika, invoice, chat, element, collab, notes,
#      ticketing, cms, store, ai, camunda) PLUS the opt-in surveys overlay
#   3. provision all content (scripts/provision.sh: OIDC clients, demo
#      users, mail rows, BPMN processes, example survey)
#   4. print a stack summary
#
# This supersedes running make up-all + the piecemeal provision scripts by
# hand. Counterpart: scripts/deprovision.sh --data (full teardown).
#
# Usage:
#   ./scripts/bootstrap.sh                 # greenfield (creates .env)
#   ./scripts/bootstrap.sh --force-env     # regenerate .env (new secrets)
#   ./scripts/bootstrap.sh --no-surveys    # skip the surveys overlay
#   ./scripts/bootstrap.sh --no-provision  # up only, then stop
#   ./scripts/bootstrap.sh --dry-run       # print the plan, change nothing
#   ./scripts/bootstrap.sh --help, -h
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC}  $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

FORCE_ENV=false
WITH_SURVEYS=true
WITH_PROVISION=true
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap.sh [options]

One-command bootstrap of the full openSME suite (all services + provisioning).

Steps:
  1. .env      generate from .env.example with random secrets (if absent)
  2. up-all    every tier service + every optional profile (make up-all)
  3. surveys   the LimeSurvey overlay (not part of up-all)
  4. provision OIDC clients, demo users, mail rows, BPMN, example survey

Options:
  --force-env      regenerate .env with fresh random secrets
  --no-surveys     skip the LimeSurvey overlay
  --no-provision   bring the stack up only, do not provision
  --dry-run        print the plan and resolved commands, change nothing
  --help, -h       show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-env)     FORCE_ENV=true; shift ;;
    --no-surveys)    WITH_SURVEYS=false; shift ;;
    --no-provision)  WITH_PROVISION=false; shift ;;
    --dry-run)       DRY_RUN=true; shift ;;
    --help|-h)       usage; exit 0 ;;
    *)               err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

if [[ "$DRY_RUN" == true ]]; then
  info "Bootstrap plan:"
  if [[ "$FORCE_ENV" == true ]]; then
    info "  1. regenerate .env from .env.example (--force-env, new random secrets)"
  elif [[ -f .env ]]; then
    info "  1. keep existing .env (--force-env to regenerate)"
  else
    info "  1. generate .env from .env.example (random secrets)"
  fi
  info "  2. make up-all (every tier + optional profiles)"
  if [[ "$WITH_SURVEYS" == true ]]; then
    info "  3. docker compose -f services/limesurvey.yml --profile surveys up -d"
  else
    info "  3. surveys overlay skipped (--no-surveys)"
  fi
  if [[ "$WITH_PROVISION" == true ]]; then
    info "  4. ./scripts/provision.sh --components all"
  else
    info "  4. provisioning skipped (--no-provision)"
  fi
  ok "dry-run — nothing was changed"
  exit 0
fi

# ── 1) environment ────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  info "No .env yet — generating from .env.example (random secrets)"
  ./scripts/init-env.sh
elif [[ "$FORCE_ENV" == true ]]; then
  ./scripts/init-env.sh --force-env
else
  ok ".env already present — keeping it (--force-env to regenerate)"
fi

# ── 2) all services ───────────────────────────────────────────────────────
info "Bringing up ALL services (every tier + optional profiles)…"
make up-all

if [[ "$WITH_SURVEYS" == true ]]; then
  info "Bringing up the surveys overlay (opensme-limesurvey + cloud-sync)…"
  docker compose --env-file .env -f docker-compose.yml -f services/limesurvey.yml \
    --profile surveys up -d
fi

# ── 3) provision ───────────────────────────────────────────────────────────
if [[ "$WITH_PROVISION" == true ]]; then
  info "Provisioning OIDC clients, demo users, mail rows, BPMN + survey…"
  ./scripts/provision.sh --components all
fi

# ── 4) summary ────────────────────────────────────────────────────────────
echo ""
ok "Bootstrap complete."
info "Portal:  $(docker port opensme-portal-web 3000 2>/dev/null | head -1 || echo 'see make status')"
info "Next:    make status | make logs  |  teardown: ./scripts/deprovision.sh"
