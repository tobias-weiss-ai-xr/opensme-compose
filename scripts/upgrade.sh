#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# openSME — Helm-free upgrades tooling
# ═══════════════════════════════════════════════════════════════
# Safe, rolling upgrade of the openSME stack:
#   docker compose pull  +  rolling `up -d` (one service at a time,
#   with a health check wait between services) guarded by the same
#   pre-flight contract checks the CI test pyramid runs
#   (make test-static: compose-check, yaml-lint, env-check,
#    secret-scan, spec compliance, contract validation).
#
# Modes:
#   scripts/upgrade.sh --help          print this usage text (no docker)
#   scripts/upgrade.sh --check         pre-flight static/contract checks only
#   scripts/upgrade.sh --pull          docker compose pull (all services)
#   scripts/upgrade.sh --up            rolling docker compose up -d
#   scripts/upgrade.sh --all           (default) --check, then --pull, then --up
#   scripts/upgrade.sh --service S     upgrade only service S
#                                      (pre-flight check + pull + up -d)
#
# Exit codes: 0 on success; 1 when a step fails (e.g. pre-flight checks
# are red, pull fails, or an `up` invocation fails). A single unhealthy
# service during the rolling phase only warns — the operator decides.
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

NL=$'\n'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

HEALTH_TIMEOUT="${UP_HEALTH_TIMEOUT:-60}"   # seconds to wait per service
HEALTH_INTERVAL="${UP_HEALTH_INTERVAL:-2}"  # seconds between health probes

# ── Compose file selection ─────────────────────────────────────────────
# Same expansion as the rest of the repo: source .env when present (so
# ${VAR} substitution and COMPOSE_FILE from .env work), while a COMPOSE_FILE
# set in the caller's environment always wins.
CALLER_COMPOSE_FILE="${COMPOSE_FILE:-}"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
COMPOSE_FILE="${CALLER_COMPOSE_FILE:-${COMPOSE_FILE:-docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml}}"
export COMPOSE_FILE

# COMPOSE may be "docker compose" (two words) or a single binary such as
# "docker-compose" — never quote it at call sites so word splitting works.
COMPOSE="${COMPOSE:-docker compose}"

# ── Helpers ────────────────────────────────────────────────────────────
die()  { printf 'upgrade: %s\n' "$*" >&2; exit 1; }
warn() { printf 'upgrade: %s\n' "$*" >&2; }

usage() {
  cat <<'EOF'
── openSME — Helm-free upgrades tooling ─────────────────────────
Safe, rolling upgrade of the openSME stack with pre-flight contract checks.

  scripts/upgrade.sh --help          print this usage text (no docker needed)
  scripts/upgrade.sh --check         run pre-flight static/contract checks only
                                     (make test-static: compose-check, yaml-lint,
                                      env-check, secret-scan, specs, contracts)
  scripts/upgrade.sh --pull          docker compose pull (all services)
  scripts/upgrade.sh --up            rolling docker compose up -d (one service
                                     at a time, health check wait between them)
  scripts/upgrade.sh --all           (default) --check, then --pull, then --up
  scripts/upgrade.sh --service S     upgrade only service S (pre-flight check +
                                     pull + rolling up -d for that service only)

Exit codes: 0 on success, 1 when any step fails. A single service that does
not turn healthy during the rolling phase only warns — the operator decides
whether to roll back (docker compose down + restore from backup).

Environment:
  COMPOSE_FILE  compose file expansion (default: docker-compose.yml:idm/
                zitadel.yml:opencloud/opencloud.yml); .env is sourced if present
  UP_HEALTH_TIMEOUT   max seconds to wait for a service to become healthy (60)
  UP_HEALTH_INTERVAL  seconds between health probes (2)
EOF
}

# ── Pre-flight contract checks ─────────────────────────────────────────
# Run the exact static layer the CI test pyramid runs. Nothing is pulled
# or restarted when this is red.
do_check() {
  echo "── pre-flight contract checks (make test-static) ──"
  if ! make test-static; then
    echo ""
    echo "✗ Pre-flight checks FAILED — aborting upgrade without touching the stack." >&2
    echo "  Fix the compose-check / yaml-lint / env-check / secret-scan / spec" >&2
    echo "  / contract failures above, then re-run." >&2
    return 1
  fi
  echo "✓ Pre-flight contract checks passed"
  return 0
}

# ── Pull images ────────────────────────────────────────────────────────
do_pull() {
  local what="$1"   # "all" or a single service name
  echo "── pulling images (compose: ${COMPOSE_FILE//$NL/ }) ──"
  if [ "$what" = "all" ]; then
    "$COMPOSE" pull
  else
    "$COMPOSE" pull "$what"
  fi
  echo "✓ images pulled (${what})"
}

# ── Wait for a service's healthcheck ───────────────────────────────────
wait_for_health() {
  local svc="$1"
  local container status elapsed=0

  container="$("$COMPOSE" ps -q "$svc" 2>/dev/null || true)"
  if [ -z "$container" ]; then
    warn "$svc: no running container to inspect — skipping health wait"
    return 0
  fi

  while [ "$elapsed" -lt "$HEALTH_TIMEOUT" ]; do
    status="$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo 'unknown')"
    case "$status" in
      healthy)    return 0 ;;
      ""|"<no value>") return 0 ;;   # no healthcheck defined — assume ok
      unhealthy)  return 1 ;;
      # "starting" and anything else: keep polling
    esac
    sleep "$HEALTH_INTERVAL"
    elapsed=$((elapsed + HEALTH_INTERVAL))
  done
  return 1
}

# ── Rolling up ─────────────────────────────────────────────────────────
# One service at a time: `up -d --no-deps <service>`, then wait for its
# healthcheck before moving on. A health failure only warns — the operator
# decides (never abort the whole upgrade).
do_up() {
  local what="$1"   # "all" or a single service name
  local services=()

  if [ "$what" = "all" ]; then
    mapfile -t services < <("$COMPOSE" config --services)
  else
    services=("$what")
  fi

  if [ "${#services[@]}" -eq 0 ]; then
    die "no services found for compose expansion: ${COMPOSE_FILE}"
  fi

  echo "── rolling up ${#services[@]} service(s) ──"
  local svc
  for svc in "${services[@]}"; do
    echo "   ↻ $COMPOSE up -d --no-deps $svc"
    "$COMPOSE" up -d --no-deps "$svc"
    if wait_for_health "$svc"; then
      echo "   ✓ $svc healthy"
    else
      warn "$svc did not become healthy within ${HEALTH_TIMEOUT}s — continuing (operator decides, see docs/UPGRADE.md)"
    fi
  done
  echo "✓ rolling upgrade complete (${what})"
}

# ── Single-service upgrade ─────────────────────────────────────────────
do_service() {
  local svc="$1"
  if [ -z "$svc" ]; then
    die "--service requires a service name (e.g. --service portal)"
  fi
  do_check || return 1
  do_pull "$svc" || return 1
  do_up "$svc" || return 1
}

# ── Full upgrade ───────────────────────────────────────────────────────
do_all() {
  do_check || return 1
  do_pull all || return 1
  do_up all || return 1
}

# ── Argument parsing ───────────────────────────────────────────────────
MODE=all
SERVICE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h)      usage; exit 0 ;;
    --check)        MODE=check ;;
    --pull)         MODE=pull ;;
    --up)           MODE=up ;;
    --all)          MODE=all ;;
    --service)      MODE=service; SERVICE="${2:-}"; shift ;;
    --service=*)    MODE=service; SERVICE="${1#--service=}" ;;
    *)
      warn "unknown option: $1"
      usage
      exit 1
      ;;
  esac
  shift
done

case "$MODE" in
  check)   do_check ;;
  pull)    do_pull all ;;
  up)      do_up all ;;
  service) do_service "$SERVICE" ;;
  all)     do_all ;;
esac
