#!/usr/bin/env bash
# ── openSME — Preflight (host/stack readiness) ─────────
# Sanity-checks the host + environment BEFORE a bootstrap/bring-up, so a
# half-dead host fails fast with an actionable message instead of the stack
# grinding to a halt mid-way (e.g. a netns-exhausted Docker daemon surfaces
# only as opaque "runc run failed ... EOF" during `make up`).
#
# Checks (FAIL = hard problem, WARN = soft / actionable):
#   ✗ docker CLI + daemon reachable
#   ✗ compose file set parses with the real engine (docker compose config -q)
#   ✗ host has a free runc/container slot (start-vs-create sanity probe)
#   ✗ .env present, keys complete, no unresolved CHANGEME_ placeholders
#   ✗ published host ports are not already taken by foreign listeners
#   ~ disk space on /var/lib/docker + repo root
#   ~ free memory
#   ~ IdP reachability (discovery URL), if configured
#
# `--strict` turns every WARN into a FAIL (exit != 0). The container-start
# probe can be skipped on a deliberately wedged host with --skip-runc.
#
# Usage:
#   ./scripts/preflight.sh                 # default: FAIL/WARN report
#   ./scripts/preflight.sh --strict        # WARNs also fail
#   ./scripts/preflight.sh --skip-runc     # skip the runc sanity probe
#   ./scripts/preflight.sh -q              # brief output (fails only)
#   ./scripts/preflight.sh --help, -h
# ═══════════════════════════════════════════════════════════════════════════

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[PREFLIGHT]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[FAIL]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1

STRICT=false
QUIET=false
SKIP_RUNC=false
FAILS=0
WARNS=0

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --strict)     STRICT=true; shift ;;
    -q|--quiet)   QUIET=true; shift ;;
    --skip-runc)  SKIP_RUNC=true; shift ;;
    --help|-h)    usage; exit 0 ;;
    *)            err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

verdict() { # verdict FAIL|WARN msg
  local kind="$1"; shift
  if [[ "$kind" == "FAIL" ]]; then
    FAILS=$((FAILS+1))
    err "  $*"
  else
    WARNS=$((WARNS+1))
    warn "  $*"
    if [[ "$STRICT" == true ]]; then FAILS=$((FAILS+1)); fi
  fi
}
summary() {
  echo ""
  if [[ $FAILS -eq 0 && $WARNS -eq 0 ]]; then
    ok "Preflight clean — host is ready to boot"
  elif [[ $FAILS -eq 0 ]]; then
    warn "${WARNS} warning(s) — review them (use --strict to harden)"
  else
    err "${FAILS} failure(s), ${WARNS} warning(s) — fix before bring-up"
  fi
  return 0
}

# ── 1) docker CLI + daemon ───────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  err "  docker CLI not found on PATH"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  verdict FAIL "docker daemon not reachable (is dockerd running? socket permission?)"
else
  [[ "$QUIET" == false ]] && ok "docker daemon reachable ($(docker info --format '{{.ServerVersion}}' 2>/dev/null))"
fi

# ── 2) compose file set parses ────────────────────────────────────────────
# Locate the bundle: repo root (greenfield) or compose/ (deployed layout).
COMPOSE_FILE="docker-compose.yml"
if [[ ! -f "$COMPOSE_FILE" && -f "compose/$COMPOSE_FILE" ]]; then
  COMPOSE_FILE="compose/$COMPOSE_FILE"
fi
if command -v docker compose >/dev/null 2>&1; then
  if [[ -f "$COMPOSE_FILE" ]]; then
    if docker compose --env-file .env -f "$COMPOSE_FILE" config -q 2>/dev/null; then
      [[ "$QUIET" == false ]] && ok "$COMPOSE_FILE parses (real compose engine)"
    else
      verdict FAIL "$COMPOSE_FILE fails 'docker compose config -q' — run 'make compose-check'"
    fi
  else
    verdict WARN "no docker-compose.yml found (root or compose/) — compose parse skipped"
  fi
else
  verdict FAIL "docker compose (v2) plugin not available"
fi

# ── 3) runc / container-start sanity ─────────────────────────────────────
# A netns-quota-exhausted daemon accepts docker create/cp fine but fails
# every start with "runc run failed ... EOF" — detect that here, early.
if [[ "$SKIP_RUNC" == true ]]; then
  [[ "$QUIET" == false ]] && info "runc sanity probe skipped (--skip-runc)"
else
  PROBE_IMG=""
  for cand in busybox:latest busybox:1.36 alpine:3.19 traefik:latest; do
    if docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -qx "$cand"; then
      PROBE_IMG="$cand"; break
    fi
  done
  if [[ -z "$PROBE_IMG" ]]; then
    verdict WARN "no tiny probe image present (busybox/alpine/traefik) — runc sanity skipped"
  else
    if docker run --rm --name preflight-runc-probe "$PROBE_IMG" true >/dev/null 2>&1; then
      [[ "$QUIET" == false ]] && ok "container start works (runc ok)"
    else
      probe_err="$(docker run --rm --name preflight-runc-probe "$PROBE_IMG" true 2>&1 | head -1)"
      if echo "$probe_err" | grep -q "runc"; then
        verdict WARN "runc cannot start containers (netns/exhaustion?) — '${probe_err//$'\n'/ }'"
        verdict WARN "  existing containers keep running, but NEW starts fail. Free a netns or"
        verdict WARN "  build images with 'docker build --network=host' to avoid new netns."
      else
        verdict WARN "container start failed: ${probe_err//$'\n'/ }"
      fi
    fi
  fi
fi

# ── 4) .env sanity ───────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  verdict FAIL "no .env yet — run ./scripts/init-env.sh (or bootstrap) first"
else
  # Only keys that the TEMPLATE knows are mandatory — historical leftovers in
  # .env (e.g. a *-era var dropped from the template) must not fail a boot.
  grep -vE '^\s*(#|$)' .env.example 2>/dev/null | sed -E 's/=.*//' | sort -u > /tmp/preflight-keys.$$
  missing=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    key="${line%%=*}"
    val="${line#*=}"
    case "$key" in
      ''|\#*) continue ;;
    esac
    if ! grep -qx "$key" /tmp/preflight-keys.$$; then
      continue  # orphan key not in the template — ignore
    fi
    if [[ -z "$val" ]] || [[ "$val" == CHANGEME_* ]]; then
      missing+=("$key")
    fi
  done < .env
  rm -f /tmp/preflight-keys.$$
  if [[ ${#missing[@]} -gt 0 ]]; then
    verdict FAIL ".env has empty/CHANGEME template values: ${missing[*]} — fix or rerun init-env --force-env"
  else
    [[ "$QUIET" == false ]] && ok ".env present, no empty/CHANGEME template values"
  fi
  leftover="$(grep -c '=CHANGEME_' .env 2>/dev/null || true)"
  if [[ "$leftover" -gt 0 ]]; then
    verdict WARN "${leftover} line(s) in .env still say CHANGEME_ (non-template leftovers; review)"
  fi
fi

# ── 5) published-port collisions ─────────────────────────────────────────
# Only a smoke signal: a port that is already listening and NOT covered by
# one of our containers is a likely conflict for a fresh bring-up.
if command -v docker compose >/dev/null 2>&1 && [[ -f .env ]]; then
  usage_json="$(docker compose --env-file .env -f "$COMPOSE_FILE" config --format json 2>/dev/null || true)"
  if [[ -n "$usage_json" && -n "$(command -v python3)" ]]; then
    ports_list="$(printf '%s' "$usage_json" | python3 -c '
import sys, json
try: c = json.load(sys.stdin)
except Exception: sys.exit(0)
seen = set()
for svc in (c.get("services") or {}).values():
    for p in (svc.get("ports") or []):
        pub = p.get("published")
        if pub and str(pub) not in seen:
            seen.add(str(pub)); print(pub)
' 2>/dev/null || true)"
    if command -v ss >/dev/null 2>&1; then
      while IFS= read -r port; do
        [[ -z "$port" ]] && continue
        if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
          covered="$(docker ps --format '{{.Ports}}' 2>/dev/null | grep -cE ":$port->" || true)"
          if [[ "$covered" -eq 0 ]]; then
            verdict WARN "host port ${port} already listening (not one of our containers) — possible conflict"
          fi
        fi
      done <<< "$ports_list"
    fi
  fi
fi

# ── 6) disk + memory (soft) ──────────────────────────────────────────────
repo_free="$(df -Pk "$ROOT" 2>/dev/null | awk 'NR==2 {print $4}')"
if [[ -n "$repo_free" ]] && (( repo_free < 10*1024*1024 )); then
  verdict WARN "repo disk space low: $(numfmt --to=iec "${repo_free}K" 2>/dev/null || echo "${repo_free} KB free")"
else
  [[ "$QUIET" == false ]] && ok "disk space OK ($(numfmt --to=iec "${repo_free}K" 2>/dev/null || echo "${repo_free} KB") free)"
fi
mem_avail="$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null)"
if [[ -n "$mem_avail" ]] && (( mem_avail < 2*1024*1024 )); then
  verdict WARN "low free memory: $((mem_avail/1024)) MB"
fi

# ── 7) IdP reachability (soft) ───────────────────────────────────────────
if command -v curl >/dev/null 2>&1; then
  set -a
  if [[ -f .env ]]; then
    # shellcheck source=/dev/null  # .env generated at deploy time
    source .env
  fi
  set +a
  idp="${ZITADEL_DOMAIN:-auth.${OPENSME_DOMAIN:-opensme.graphwiz.ai}}"
  if curl -sk --max-time 8 "https://$idp/healthz" >/dev/null 2>&1; then
    [[ "$QUIET" == false ]] && ok "IdP reachable (https://$idp/healthz)"
  else
    verdict WARN "IdP $idp not reachable yet — skip this check when the IdP is part of the same bring-up"
  fi
fi

summary
[[ $FAILS -eq 0 ]] && exit 0 || exit 1
