#!/usr/bin/env bash
# ── openSME — Provision ─────────────────────────────
# Idempotent provisioning of the openSME suite against a RUNNING stack.
# Safe to re-run at any time — every component is a no-op when already done.
#
#   oidc     register every OIDC client (opencloud, portal, notes, synapse,
#            ticketing, sogo) — scripts/zitadel-client.sh --app <name>
#   users    seed the 12-person demo company into every user store
#            (scripts/provision-demo-users.sh --component all)
#   mail     Zitadel + Stalwart rows the SSO/mail legs need
#            (scripts/provision-demo-users.sh --component zitadel,stalwart)
#   bpmn     deploy seed BPMN processes (bootstrap/bpmn-deploy.sh)
#   surveys  seed the example LimeSurvey (bootstrap/survey-deploy.sh)
#
# Components whose stack slice isn't running are skipped with a hint
# (e.g. bpmn needs the opensme-camunda container; surveys needs postgres).
# This is the counterpart of scripts/deprovision.sh.
#
# Usage:
#   ./scripts/provision.sh                        # all components, best effort
#   ./scripts/provision.sh --components oidc,users
#   ./scripts/provision.sh --skip-oidc --skip-bpmn
#   ./scripts/provision.sh --dry-run
#   ./scripts/provision.sh --help
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC}  $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

ALL_COMPONENTS=(oidc users mail bpmn surveys)
declare -A ENABLED=( [oidc]=true [users]=true [mail]=true [bpmn]=true [surveys]=true )
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: scripts/provision.sh [options]

Idempotently provision the openSME suite against a RUNNING stack.

Components (default: all):
  oidc      register every OIDC client in Zitadel (zitadel-client.sh)
  users     seed demo users into every user store (provision-demo-users.sh)
  mail      Zitadel + Stalwart rows the SSO/mail legs need
  bpmn      deploy seed BPMN processes (needs the camunda profile)
  surveys   seed the example LimeSurvey (needs the surveys overlay)

Options:
  --components LIST   comma-separated subset, e.g. oidc,users
  --skip-<name>       disable one component (--skip-oidc, --skip-users,
                      --skip-mail, --skip-bpmn, --skip-surveys)
  --dry-run           print the plan, change nothing
  --help, -h          show this help

Exit code is 0 even if a component was skipped (best effort); a component
that partially fails is reported as [WARN] and provisioning continues.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --components)
      [[ -n "${2:-}" ]] || { err "--components needs a value (comma-separated: ${ALL_COMPONENTS[*]})"; exit 1; }
      IFS=',' read -r -a EXPLICIT <<< "$2"
      shift 2 ;;
    --skip-oidc)     ENABLED[oidc]=false;     shift ;;
    --skip-users)    ENABLED[users]=false;    shift ;;
    --skip-mail)     ENABLED[mail]=false;     shift ;;
    --skip-bpmn)     ENABLED[bpmn]=false;     shift ;;
    --skip-surveys)  ENABLED[surveys]=false;  shift ;;
    --dry-run)       DRY_RUN=true; shift ;;
    --help|-h)       usage; exit 0 ;;
    *)               err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

if [[ -n "${EXPLICIT:-}" ]]; then
  for c in "${!ENABLED[@]}"; do ENABLED[$c]=false; done
  for c in "${EXPLICIT[@]}"; do
    if [[ ! -v ENABLED[$c] ]]; then
      err "Unknown component '$c' (use: ${ALL_COMPONENTS[*]})"
      exit 1
    fi
    ENABLED[$c]=true
  done
fi

# ── helpers ──────────────────────────────────────────────────────────────
running() { docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$1"; }

# ── components ────────────────────────────────────────────────────────────
comp_oidc() {
  local apps=(opencloud portal notes synapse ticketing sogo)
  local a
  for a in "${apps[@]}"; do
    if [[ "$DRY_RUN" == true ]]; then
      info "  would register OIDC client '${a}'"
      continue
    fi
    if bash scripts/zitadel-client.sh --app "$a" >/dev/null; then
      ok "OIDC client '${a}' is registered"
    else
      warn "OIDC client '${a}' failed — IdP slice down? rerun after 'make up'"
    fi
  done
}

comp_users() {
  if [[ "$DRY_RUN" == true ]]; then
    info "  would seed demo users into every user store"
    return 0
  fi
  if bash scripts/provision-demo-users.sh --wait --component all; then
    ok "demo users seeded"
  else
    warn "demo-user provisioning incomplete (some user stores down?)"
  fi
}

comp_mail() {
  local a
  if [[ "$DRY_RUN" == true ]]; then
    info "  would provision Zitadel + Stalwart mail rows"
    return 0
  fi
  for a in zitadel stalwart; do
    bash scripts/provision-demo-users.sh --component "$a" || warn "mail provisioning for '${a}' incomplete"
  done
  ok "mail provisioning finished"
}

comp_bpmn() {
  if [[ "$DRY_RUN" == true ]]; then
    info "  would deploy seed BPMN processes"
    return 0
  fi
  if running opensme-camunda; then
    bash bootstrap/bpmn-deploy.sh
  else
    warn "opensme-camunda not running — skipping BPMN (start with --profile camunda)"
  fi
}

comp_surveys() {
  if [[ "$DRY_RUN" == true ]]; then
    info "  would seed the example LimeSurvey"
    return 0
  fi
  if running opensme-postgres; then
    bash bootstrap/survey-deploy.sh
  else
    warn "opensme-postgres not running — skipping surveys (start with --profile surveys)"
  fi
}

# ── plan / run ────────────────────────────────────────────────────────────
info "Provision plan:"
for c in "${ALL_COMPONENTS[@]}"; do
  if [[ "${ENABLED[$c]}" == true ]]; then
    info "  ✓ ${c}"
  else
    info "  – ${c} (skipped)"
  fi
done
if [[ "$DRY_RUN" == true ]]; then
  ok "dry-run — nothing was changed"
  exit 0
fi
echo ""

for c in "${ALL_COMPONENTS[@]}"; do
  if [[ "${ENABLED[$c]}" != true ]]; then
    continue
  fi
  info "── provisioning: ${c} ──"
  "comp_${c}"
done

echo ""
ok "Provisioning complete. Counterpart: ./scripts/deprovision.sh"
