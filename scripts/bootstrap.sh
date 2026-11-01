#!/usr/bin/env bash
# ── openSME — Bootstrap (all services) ─────────────
# One-command greenfield bootstrap of the FULL openSME suite:
#
#   1. generate .env from the template (random secrets; refuses to clobber)
#   2. bring up EVERY service — every tier + every optional profile
#      (make up-all: paperless, tika, invoice, chat, element, collab, notes,
#      ticketing, cms, store, ai, camunda) PLUS the opt-in surveys overlay
#   3. provision all content (scripts/provision.sh: OIDC clients, demo
#      users, mail rows, BPMN processes, example survey, seed documents)
#   4. (opt-in --verify) verify-stack.sh — container health + endpoints + SSO
#
# Host/stack preflight (scripts/preflight.sh) runs first and fail-fasts on
# real host problems (daemon down, netns-exhausted runc, broken compose,
# missing env). Completed steps are recorded in .bootstrap-state so
# `--resume` continues after an interruption instead of restarting.
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
WITH_VERIFY=false
WITH_PREFLIGHT=true
RESUME=false
DRY_RUN=false

STATE_FILE="$SCRIPT_DIR/../.bootstrap-state"

step_done()   { [[ -s "$STATE_FILE" ]] && grep -qx "$1" "$STATE_FILE"; }
mark_done()   { printf '%s\n' "$1" >> "$STATE_FILE"; }

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
  --no-verify      skip the post-bootstrap verify step (default)
  --verify         run { scripts/verify-stack.sh } after provisioning
  --preflight      run host/stack preflight first (default on; fail-fast)
  --skip-preflight host/stack preflight + its fail-fast gate
  --resume         skip steps already finished (.bootstrap-state checkpoints)
  --dry-run        print the plan and resolved commands, change nothing
  --help, -h       show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-env)     FORCE_ENV=true; shift ;;
    --no-surveys)    WITH_SURVEYS=false; shift ;;
    --no-provision)  WITH_PROVISION=false; shift ;;
    --verify)        WITH_VERIFY=true; shift ;;
    --no-verify)     WITH_VERIFY=false; shift ;;
    --preflight)     WITH_PREFLIGHT=true; shift ;;
    --skip-preflight) WITH_PREFLIGHT=false; shift ;;
    --resume)        RESUME=true; shift ;;
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
  if [[ "$WITH_VERIFY" == true ]]; then
    info "  5. ./scripts/verify-stack.sh (containers + endpoints + SSO)"
  else
    info "  5. verify skipped (--verify to enable)"
  fi
  if [[ "$RESUME" == true ]]; then
    info "  resume mode: completed steps (.bootstrap-state) will be skipped"
  fi
  ok "dry-run — nothing was changed"
  exit 0
fi

# ── 0) preflight (fail-fast on real host problems) ───────────────────────
if [[ "$WITH_PREFLIGHT" == true ]]; then
  if [[ "$RESUME" == true ]] && step_done preflight; then
    info "preflight already passed earlier (resume) — skipping"
  else
    info "Running host/stack preflight (--skip-preflight to bypass)…"
    if ./scripts/preflight.sh; then
      mark_done preflight
    else
      err "Preflight reported failures — aborting. Fix the host first, or rerun with --skip-preflight."
      exit 1
    fi
  fi
fi

# ── 1) environment ────────────────────────────────────────────────────────
if [[ "$RESUME" == true ]] && step_done env; then
  info "env already handled (resume) — skipping"
else
  if [[ ! -f .env ]]; then
    info "No .env yet — generating from .env.example (random secrets)"
    ./scripts/init-env.sh
  elif [[ "$FORCE_ENV" == true ]]; then
    ./scripts/init-env.sh --force-env
  else
    ok ".env already present — keeping it (--force-env to regenerate)"
  fi
  mark_done env
fi

# ── 2) all services ───────────────────────────────────────────────────────
if [[ "$RESUME" == true ]] && step_done up; then
  info "stack already brought up (resume) — skipping 'make up-all'"
else
  info "Bringing up ALL services (every tier + optional profiles)…"
  make up-all
  mark_done up
fi

if [[ "$WITH_SURVEYS" == true ]]; then
  if [[ "$RESUME" == true ]] && step_done surveys; then
    info "surveys overlay already up (resume) — skipping"
  else
    info "Bringing up the surveys overlay (opensme-limesurvey + cloud-sync)…"
    docker compose --env-file .env -f docker-compose.yml -f services/limesurvey.yml \
      --profile surveys up -d
    mark_done surveys
  fi
fi

# ── 3) provision ───────────────────────────────────────────────────────────
if [[ "$WITH_PROVISION" == true ]]; then
  if [[ "$RESUME" == true ]] && step_done provision; then
    info "provisioning already finished (resume) — skipping"
  else
    info "Provisioning OIDC clients, demo users, mail rows, BPMN, survey + seed content…"
    ./scripts/provision.sh --components all
    mark_done provision
  fi
fi

# ── 3b) verify (opt-in) ────────────────────────────────────────────────────
if [[ "$WITH_VERIFY" == true ]]; then
  if [[ "$RESUME" == true ]] && step_done verify; then
    info "verify already passed (resume) — skipping"
  else
    info "Verifying the booted stack (containers + endpoints + SSO)…"
    if ./scripts/verify-stack.sh; then
      mark_done verify
    else
      err "Verification reported failures — stack is up but not clean."
      exit 1
    fi
  fi
fi

# ── 4) summary ────────────────────────────────────────────────────────────
echo ""
ok "Bootstrap complete."
info "Portal:  $(docker port opensme-portal-web 3000 2>/dev/null | head -1 || echo 'see make status')"
info "Next:    make status | make logs  |  verify: ./scripts/verify-stack.sh"
info "State:    $(wc -l < "$STATE_FILE" 2>/dev/null || echo 0) bootstrap checkpoint(s) recorded (--resume to continue)"
