#!/usr/bin/env bash
# ── openSME — Verify stack (post-bootstrap health + SSO + smoke) ──
# Runtime verification of a deployed openSME stack:
#
#   1. container health loop  — every running opensme-* container must be
#                               healthy (or at least Running) after retries
#   2. endpoint probes        — the public service doors answer 2xx/3xx
#                               (per-service entry points, same list the
#                               bootstrap docs advertise)
#   3. SSO matrix             — scripts/sso-check.sh (per-app SSO entries)
#   4. smoke (optional)       — tests/03-smoke/run.py if runnable
#
# Exits 0 only when container-health + endpoint probes + SSO all pass;
# smoke failures are reported but not fatal (different duty cycle).
#
# Usage:
#   ./scripts/verify-stack.sh                # full verification
#   ./scripts/verify-stack.sh --no-sso       # skip SSO matrix
#   ./scripts/verify-stack.sh --smoke-only   # only the smoke suite
#   ./scripts/verify-stack.sh --dry-run      # show what would be probed
#   ./scripts/verify-stack.sh --help, -h
# ═══════════════════════════════════════════════════════════════════════════

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[VERIFY]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC}  $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1

WITH_SSO=true
SMOKE_ONLY=false
DRY_RUN=false
FAILS=0

# The compose project may live in a subdirectory (ansible deploys the repo
# under /opt/opensme-compose/compose while the verify scripts sit in
# /opt/opensme-compose/scripts). Docker compose locates the project via
# COMPOSE_FILE/COMPOSE_PROJECT_DIR; point it at the real project so both the
# container loop and the smoke suite resolve the live stack regardless of cwd.
COMPOSE_PROJECT="${COMPOSE_PROJECT:-}"
if [[ -z "$COMPOSE_PROJECT" && -f "${ROOT}/compose/docker-compose.yml" ]]; then
  COMPOSE_PROJECT="${ROOT}/compose"
elif [[ -z "$COMPOSE_PROJECT" && -f "${ROOT}/docker-compose.yml" ]]; then
  COMPOSE_PROJECT="${ROOT}"
fi
if [[ -n "$COMPOSE_PROJECT" ]]; then
  export COMPOSE_PROJECT_DIR="$COMPOSE_PROJECT"
  [[ -f "$COMPOSE_PROJECT/.env" ]] && export COMPOSE_ENV_FILES="$COMPOSE_PROJECT/.env"
  info "compose project: $COMPOSE_PROJECT"
fi

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-sso)     WITH_SSO=false; shift ;;
    --smoke-only) SMOKE_ONLY=true; shift ;;
    --dry-run)    DRY_RUN=true; shift ;;
    --help|-h)    usage; exit 0 ;;
    *)            err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

if [[ -f .env ]]; then
  set -a
  # shellcheck source=/dev/null  # .env generated at deploy time
  source .env
  set +a
fi
DOMAIN="${OPENSME_DOMAIN:-opensme.graphwiz.ai}"
IDP_DOMAIN="${ZITADEL_DOMAIN:-auth.${DOMAIN}}"
PORTAL_DOMAIN="${PORTAL_DOMAIN:-home.${DOMAIN}}"
OPENCLOUD_DOMAIN="${OPENCLOUD_DOMAIN:-cloud.${DOMAIN}}"
MATRIX_DOMAIN="${MATRIX_DOMAIN:-matrix.${DOMAIN}}"
NOTES_DOMAIN="${NOTES_DOMAIN:-notes.${DOMAIN}}"
SOGO_DOMAIN="${SOGO_DOMAIN:-webmail.${DOMAIN}}"
PAPERLESS_DOMAIN="${PAPERLESS_DOMAIN:-paperless.${DOMAIN}}"
CAMUNDA_DOMAIN="${CAMUNDA_DOMAIN:-bpm.${DOMAIN}}"

# ── container health loop ─────────────────────────────────────────────────
check_containers() {
  info "Container health loop (opensme-*)…"
  local names ready
  names="$(docker ps --format '{{.Names}}' 2>/dev/null | grep '^opensme-' | sort -u || true)"
  if [[ -z "$names" ]]; then
    err "  no opensme-* containers are running"
    FAILS=$((FAILS+1))
    return 1
  fi
  while IFS= read -r c; do
    [[ -z "$c" ]] && continue
    ready=""
    for _ in $(seq 1 20); do
      st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null || echo dead)"
      case "$st" in
        healthy) ready=ok; break ;;
        running) ready=ok; break ;;   # no healthcheck defined
        unhealthy) break ;;
        *) sleep 3 ;;
      esac
    done
    if [[ "$ready" == ok ]]; then
      ok "  container ${c} $(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}running{{end}}' "$c")"
    else
      err "  container ${c} not healthy: $(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null || echo 'gone')"
      FAILS=$((FAILS+1))
    fi
  done <<< "$names"
}

# ── endpoint probes ───────────────────────────────────────────────────────
# name url [expected-status-pattern]
probe() {
  local name="$1" url="$2" pattern="${3:-2|3}"
  if [[ "$DRY_RUN" == true ]]; then
    info "  probe ${name} -> ${url}"
    return 0
  fi
  local code
  code="$(curl -sk --max-time 15 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
  if echo "$code" | grep -qE "^($pattern)"; then
    ok "  ${name} ${url} -> ${code}"
  else
    err "  ${name} ${url} -> ${code} (expected ^${pattern})"
    FAILS=$((FAILS+1))
  fi
}

check_endpoints() {
  info "Endpoint probes…"
  probe "IdP"        "https://${IDP_DOMAIN}/healthz"
  probe "Portal"     "https://${PORTAL_DOMAIN}/"
  probe "OpenCloud"  "https://${OPENCLOUD_DOMAIN}/"
  probe "Webmail"    "https://${SOGO_DOMAIN}/de/"
  probe "Paperless"  "https://${PAPERLESS_DOMAIN}/accounts/login/"
  probe "Notes"      "https://${NOTES_DOMAIN}/api/v1.0/authenticate/" "3"
  probe "Matrix"     "https://${MATRIX_DOMAIN}/_matrix/client/versions"
}

# ── SSO matrix ────────────────────────────────────────────────────────────
check_sso() {
  if [[ "$WITH_SSO" != true ]]; then
    info "SSO matrix skipped (--no-sso)"
    return 0
  fi
  info "SSO matrix (scripts/sso-check.sh)…"
  if bash scripts/sso-check.sh; then
    ok "  SSO matrix passed"
  else
    err "  SSO matrix failed — fix before declaring the stack verified"
    FAILS=$((FAILS+1))
  fi
}

# ── smoke (non-fatal) ─────────────────────────────────────────────────────
check_smoke() {
  info "Smoke suite (tests/03-smoke/run.py)…"
  if ! python3 -c 'import requests, urllib3' >/dev/null 2>&1; then
    warn "  smoke deps (requests) unavailable locally — skipped"
    return 0
  fi
  if [[ "$DRY_RUN" == true ]]; then
    info "  would run: python3 tests/03-smoke/run.py ${DOMAIN:-localhost}"
    return 0
  fi
  if python3 tests/03-smoke/run.py "${DOMAIN:-localhost}" >/dev/null 2>&1; then
    ok "  smoke suite passed"
  else
    warn "  smoke suite reported failures (non-fatal here; run it directly for details)"
  fi
}

if [[ "$DRY_RUN" == true ]]; then
  info "verify plan:"
  [[ "$SMOKE_ONLY" != true ]] && { check_containers; check_endpoints; [[ "$WITH_SSO" == true ]] && info "  SSO matrix via scripts/sso-check.sh"; }
  check_smoke
  ok "dry-run — nothing probed"
  exit 0
fi

if [[ "$SMOKE_ONLY" == true ]]; then
  check_smoke
  exit 0
fi

check_containers
check_endpoints
check_sso
check_smoke

echo ""
if [[ $FAILS -eq 0 ]]; then
  ok "Stack verification passed (containers + endpoints + SSO)"
  exit 0
else
  err "${FAILS} verification failure(s)"
  exit 1
fi
