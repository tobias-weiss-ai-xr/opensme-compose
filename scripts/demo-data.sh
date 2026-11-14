#!/usr/bin/env bash
# ── openSME — Demo Data (provision + deprovision, all apps) ─────────────
#
# Single entry point for the FULL demo-data lifecycle of an openSME stack:
# OIDC clients, demo users across every user store, mail rows, BPMN/survey/
# content seeds, OpenCloud spaces, paperless permissions, SOGo CalDAV,
# invoice demo tenant, calendar event types, and the tobias admin coverage.
#
#   ./scripts/demo-data.sh provision              # everything, idempotent
#   ./scripts/demo-data.sh deprovision            # state only (stack stays up)
#   ./scripts/demo-data.sh deprovision --data     # + destroy volumes (guarded)
#
# It reuses the per-app provisioners (provision.sh, deprovision.sh and the
# scripts/provision-*.sh helpers) instead of duplicating their logic, so
# idempotency and --dry-run semantics are inherited.
#
# Options (forwarded where applicable):
#   --dry-run          print the plan, change nothing
#   --skip-<name>      skip a component (see --list)
#   --components LIST  comma subset of the core components
#   --data             (deprovision) also `docker compose down -v`
#   --yes              (deprovision --data) skip the confirmation prompt
#   --list             print the component inventory and exit
#   --help, -h         show this help
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[demo-data]${NC} $*"; }
ok()   { echo -e "${GREEN}[demo-data]${NC} $*"; }
warn() { echo -e "${YELLOW}[demo-data]${NC} $*"; }
err()  { echo -e "${RED}[demo-data]${NC} $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

MODE=""
DRY_RUN=false
LIST_ONLY=false

usage() {
  cat <<'EOF'
Usage: scripts/demo-data.sh {provision|deprovision} [options]

Modes:
  provision      seed demo data into every app (idempotent)
  deprovision    remove demo data (state only by default)

Core components (provision.sh / deprovision.sh):
  oidc users mail bpmn surveys content  (provision)
  surveys bpmn users oidc              (deprovision)

Extra app components (always attempted when the owning container is up):
  opencloud        spaces + system users (provision-opencloud.sh)
  paperless-perms  oidc-users group + backfill (provision-paperless-perms.sh)
  sogo-caldav      CalDAV address book sync (provision-sogo-caldav.sh)
  invoice-demo     GraphWiz demo tenant (provision-invoice-demo.sh)
  calendar         event types + vc room (calendar-setup.sh)
  admin            tobias admin coverage (provision-admin.sh)

Options:
  --dry-run        print the plan, change nothing
  --skip-<name>    skip a component (--skip-opencloud, --skip-invoice-demo, ...)
  --components L   comma subset of the CORE components only
  --data           (deprovision) also `docker compose down -v`
  --yes            (deprovision --data) skip the confirmation prompt
  --list           print this inventory and exit
  --help, -h       show this help

Examples:
  ./scripts/demo-data.sh provision
  ./scripts/demo-data.sh provision --dry-run
  ./scripts/demo-data.sh provision --skip-invoice-demo
  ./scripts/demo-data.sh deprovision
  ./scripts/demo-data.sh deprovision --data --yes
EOF
}

# ── parse mode + options ─────────────────────────────────────────────────
if [[ $# -eq 0 ]]; then
  usage; exit 1
fi
# allow --list / --help without a mode
case "$1" in
  --list) LIST_ONLY=true; shift ;;
  --help|-h) usage; exit 0 ;;
esac
if [[ "$LIST_ONLY" != true && ( $# -eq 0 || ( "$1" != "provision" && "$1" != "deprovision" ) ) ]]; then
  if [[ "$LIST_ONLY" != true ]]; then
    err "First argument must be 'provision' or 'deprovision' (got '${1:-}')"; usage; exit 1
  fi
fi
if [[ "$LIST_ONLY" != true ]]; then
  case "$1" in
    provision|deprovision) MODE="$1"; shift ;;
  esac
fi

CORE_ARGS=()
SKIP_EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)    DRY_RUN=true; CORE_ARGS+=("$1"); shift ;;
    --data|--yes) CORE_ARGS+=("$1"); shift ;;
    --components) CORE_ARGS+=("$1" "$2"); shift 2 ;;
    --skip-*)     SKIP_EXTRA+=("${1#--skip-}"); CORE_ARGS+=("$1"); shift ;;
    --list)       LIST_ONLY=true; shift ;;
    --help|-h)    usage; exit 0 ;;
    *)            err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

if [[ "$LIST_ONLY" == true ]]; then
  usage; exit 0
fi

skip_extra() {
  local name="$1"
  for s in "${SKIP_EXTRA[@]}"; do
    [[ "$s" == "$name" ]] && return 0
  done
  return 1
}

run() {
  # run CMD...; never abort the whole shebang on a single component failure
  if [[ "$DRY_RUN" == true ]]; then
    info "  would run: $*"
    return 0
  fi
  if "$@"; then
    ok "  ok: $*"
  else
    warn "  failed: $* (continuing)"
  fi
}

running() { docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$1"; }

info "Mode: ${MODE}"
[[ "$DRY_RUN" == true ]] && info "Dry run — nothing will be changed"
echo ""

# ── core (existing orchestrators) ────────────────────────────────────────
info "── core components (${MODE}) ──"
if [[ "$MODE" == "provision" ]]; then
  run bash scripts/provision.sh "${CORE_ARGS[@]}"
else
  run bash scripts/deprovision.sh "${CORE_ARGS[@]}"
fi
echo ""

# ── extra app components ─────────────────────────────────────────────────
info "── app demo data (${MODE}) ──"

# opencloud
if ! skip_extra opencloud; then
  if [[ "$MODE" == "provision" ]]; then
    if running opensme-opencloud; then
      run bash scripts/provision-opencloud.sh ${DRY_RUN:+--dry-run}
    else
      warn "opensme-opencloud not running — skipping opencloud"
    fi
  else
    if running opensme-opencloud; then
      run bash scripts/provision-opencloud.sh --clean ${DRY_RUN:+--dry-run}
    else
      warn "opensme-opencloud not running — skipping opencloud"
    fi
  fi
fi

# paperless permissions
if ! skip_extra paperless-perms && ! skip_extra paperless; then
  if running opensme-paperless; then
    if [[ "$MODE" == "provision" ]]; then
      run bash scripts/provision-paperless-perms.sh
    else
      warn "paperless permissions are not auto-revoked (re-run provision to reset)"
    fi
  else
    warn "opensme-paperless not running — skipping paperless-perms"
  fi
fi

# sogo caldav
if ! skip_extra sogo-caldav && ! skip_extra sogo; then
  if running opensme-sogo6-postgres; then
    if [[ "$MODE" == "provision" ]]; then
      run bash scripts/provision-sogo-caldav.sh
    else
      warn "sogo caldav sync is not auto-reversed (idempotent re-run is safe)"
    fi
  else
    warn "opensme-sogo6-postgres not running — skipping sogo-caldav"
  fi
fi

# invoice demo tenant
if ! skip_extra invoice-demo && ! skip_extra invoice; then
  if running opensme-invoiceninja; then
    if [[ "$MODE" == "provision" ]]; then
      run bash scripts/provision-invoice-demo.sh ${DRY_RUN:+--dry-run}
    else
      run bash scripts/provision-invoice-demo.sh --clean ${DRY_RUN:+--dry-run}
    fi
  else
    warn "opensme-invoiceninja not running — skipping invoice-demo"
  fi
fi

# calendar event types
if ! skip_extra calendar; then
  if running opensme-calendar; then
    if [[ "$MODE" == "provision" ]]; then
      run bash scripts/calendar-setup.sh
    else
      warn "calendar event types are not auto-removed (re-run provision to reset)"
    fi
  else
    warn "opensme-calendar not running — skipping calendar"
  fi
fi

# tobias admin coverage
if ! skip_extra admin; then
  if running opensme-zitadel; then
    if [[ "$MODE" == "provision" ]]; then
      run bash scripts/provision-admin.sh ${DRY_RUN:+--dry-run}
    else
      warn "admin coverage is not auto-revoked (re-run provision to reset)"
    fi
  else
    warn "opensme-zitadel not running — skipping admin"
  fi
fi

echo ""
ok "Demo data ${MODE} complete."