#!/usr/bin/env bash
# ── openSME — Deprovision ───────────────────────────
# Reverses what scripts/provision.sh / scripts/bootstrap.sh created, in
# reverse dependency order, and (optionally) destroys the stack + volumes.
#
#   surveys  drop the seeded LimeSurvey example (lime_* rows + response table)
#   bpmn     undeploy every seed process (deployment-source=opensme-bootstrap)
#   users    delete the demo users (best effort per user store)
#   oidc     delete the Zitadel OIDC clients + their .env keys
#   data(*)  docker compose down -v — DESTROYS ALL VOLUME DATA (guarded)
#
# Default is STATE-ONLY deprovision: provisioned state is removed, the
# stack and its volumes stay (re-provisionable with ./scripts/provision.sh).
# Pass --data for a full teardown (containers + volumes gone).
#
# Usage:
#   ./scripts/deprovision.sh                    # state only
#   ./scripts/deprovision.sh --data             # state + destroy volumes
#   ./scripts/deprovision.sh --skip-oidc --skip-users
#   ./scripts/deprovision.sh --dry-run
#   ./scripts/deprovision.sh --yes              # skip the --data confirmation
#   ./scripts/deprovision.sh --help
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC}  $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

ALL_COMPONENTS=(surveys bpmn users oidc)
declare -A ENABLED=( [surveys]=true [bpmn]=true [users]=true [oidc]=true )
WITH_DATA=false
YES=false
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: scripts/deprovision.sh [options]

Remove the state that scripts/provision.sh created (idempotent — a
component that was never provisioned simply reports "nothing to do").

Components (default: all):
  surveys   delete the seeded LimeSurvey example
  bpmn      undeploy seed BPMN deployments (source opensme-bootstrap)
  users     delete demo users (best effort per user store)
  oidc      delete Zitadel OIDC clients + their .env keys

Options:
  --data           full teardown: ALSO 'docker compose down -v'
                   (containers + ALL volume data are destroyed)
  --yes            skip the --data confirmation prompt
  --skip-<name>    disable one component (--skip-surveys, --skip-bpmn,
                   --skip-users, --skip-oidc)
  --dry-run        print the plan, change nothing
  --help, -h       show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data)          WITH_DATA=true; shift ;;
    --yes)           YES=true; shift ;;
    --skip-surveys)  ENABLED[surveys]=false; shift ;;
    --skip-bpmn)     ENABLED[bpmn]=false;    shift ;;
    --skip-users)    ENABLED[users]=false;   shift ;;
    --skip-oidc)     ENABLED[oidc]=false;    shift ;;
    --dry-run)       DRY_RUN=true; shift ;;
    --help|-h)       usage; exit 0 ;;
    *)               err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

# ── helpers ──────────────────────────────────────────────────────────────
running() { docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$1"; }

# ── components (reverse dependency order) ────────────────────────────────
comp_surveys() {
  if [[ "$DRY_RUN" == true ]]; then
    info "  would delete the seeded LimeSurvey example"
    return 0
  fi
  if running opensme-postgres; then
    bash bootstrap/survey-deploy.sh --delete || warn "survey deletion failed"
  else
    warn "opensme-postgres not running — nothing to delete (surveys live in the DB)"
  fi
}

comp_bpmn() {
  if [[ "$DRY_RUN" == true ]]; then
    info "  would undeploy seed BPMN deployments"
    return 0
  fi
  if running opensme-camunda; then
    bash bootstrap/bpmn-deploy.sh --undeploy all || warn "BPMN undeploy failed"
  else
    warn "opensme-camunda not running — nothing to undeploy"
  fi
}

comp_users() {
  if [[ "$DRY_RUN" == true ]]; then
    info "  would delete demo users (best effort per store)"
    return 0
  fi
  if bash scripts/provision-demo-users.sh --wait --clean >/dev/null 2>&1; then
    ok "demo users deleted (where the store supports cleanup)"
  else
    warn "demo-user deletion incomplete (some stores only reset on --data)"
  fi
}

comp_oidc() {
  local apps=(opencloud portal notes synapse ticketing sogo)
  local a
  for a in "${apps[@]}"; do
    if [[ "$DRY_RUN" == true ]]; then
      info "  would delete OIDC client '${a}'"
      continue
    fi
    if running opensme-zitadel; then
      bash scripts/zitadel-client.sh --remove --app "$a" || warn "OIDC client '${a}' removal failed"
    else
      warn "opensme-zitadel not running — skipping OIDC client '${a}'"
    fi
  done
}

comp_data() {
  if [[ "$DRY_RUN" == true ]]; then
    err "dry-run: would run 'docker compose down -v' (all volume data gone)"
    return 0
  fi
  if [[ "$YES" != true ]]; then
    read -r -p "DESTROY all containers, networks and VOLUMES? type 'yes' to continue: " ans
    [[ "$ans" == "yes" ]] || { warn "aborted — volumes kept"; exit 0; }
  fi
  make down-volumes
}

# ── plan ──────────────────────────────────────────────────────────────────
info "Deprovision plan:"
for c in "${ALL_COMPONENTS[@]}"; do
  if [[ "${ENABLED[$c]}" == true ]]; then
    info "  ✓ ${c}"
  else
    info "  – ${c} (skipped)"
  fi
done
if [[ "$WITH_DATA" == true ]]; then
  info "  ✓ data (down -v, destroys volumes)"
else
  info "  – data (add --data for volume destruction)"
fi
if [[ "$DRY_RUN" == true ]]; then
  ok "dry-run — nothing was changed"
  exit 0
fi
echo ""

for c in "${ALL_COMPONENTS[@]}"; do
  if [[ "${ENABLED[$c]}" == true ]]; then
    info "── deprovisioning: ${c} ──"
    "comp_${c}"
  fi
done

if [[ "$WITH_DATA" == true ]]; then
  info "── deprovisioning: data (down -v) ──"
  comp_data
fi

echo ""
ok "Deprovisioning complete. To provision again: ./scripts/provision.sh"
