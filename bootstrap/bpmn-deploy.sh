#!/usr/bin/env bash
# bootstrap/bpmn-deploy.sh — deploy (or undeploy) seed BPMN processes.
#
# Usage:
#   ./bootstrap/bpmn-deploy.sh                    # auto-detect (docker exec or BPM_URL)
#   BPM_URL=https://bpm.example.org ./bootstrap/bpmn-deploy.sh
#   ./bootstrap/bpmn-deploy.sh --undeploy all     # remove every seed deployment
#   ./bootstrap/bpmn-deploy.sh --undeploy invoice # remove matching deployments
#
# Deploys every .bpmn file in bootstrap/bpmn/ to the engine's REST API
# (POST /engine-rest/deployment/create). Idempotent: re-running updates
# existing deployments by name. --undeploy removes the deployments tagged
# deployment-source=opensme-bootstrap (cascade deletes instances).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BPMN_DIR="${SCRIPT_DIR}/bpmn"

UNDEPLOY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --undeploy)
      UNDEPLOY="${2:-}"
      [[ -n "$UNDEPLOY" ]] || { echo "error: --undeploy needs a key or 'all'" >&2; exit 1; }
      shift 2 ;;
    --help|-h)
      echo "Usage: $0 [--undeploy all|<key>]" >&2
      exit 0 ;;
    *) echo "error: unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! command -v curl >/dev/null 2>&1; then
  echo "error: curl is required" >&2
  exit 1
fi

# ── Resolve the engine URL ─────────────────────────
# 1. A locally RUNNING opensme-camunda container (docker exec) — preferred:
#    immune to edge quirks (the fleet Traefik answers multipart deployment
#    POSTs with 418/parse-mangling; seen live 2026-10-30) and to BPM_URL
#    leakage from a sourced .env (BPM_URL is the E2E suite's public URL).
# 2. Explicit BPM_URL env var (e.g. https://bpm.example.org) — remote engine
# 3. Fallback: http://localhost:8080 (works if port is published)

BPM_URL="${BPM_URL:-}"
USE_DOCKER_EXEC=false

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^opensme-camunda$'; then
  echo "Found opensme-camunda container — deploying via docker exec"
  USE_DOCKER_EXEC=true
elif [ -n "$BPM_URL" ]; then
  echo "Using BPM_URL: ${BPM_URL}"
else
  BPM_URL="http://localhost:8080"
  echo "Using default BPM_URL: ${BPM_URL}"
fi

# ── Wait for the engine ────────────────────────────
echo "Waiting for Operaton engine…"
if [ "$USE_DOCKER_EXEC" = true ]; then
  for i in $(seq 1 30); do
    if docker exec opensme-camunda wget -q -O /dev/null http://localhost:8080/engine-rest/engine 2>/dev/null; then
      echo "  engine is up (via docker exec)"
      break
    fi
    if [ "$i" -eq 30 ]; then
      echo "error: engine not reachable in opensme-camunda container" >&2
      exit 1
    fi
    sleep 2
  done
else
  for i in $(seq 1 30); do
    if curl -sf "${BPM_URL}/engine-rest/engine" >/dev/null 2>&1; then
      echo "  engine is up"
      break
    fi
    if [ "$i" -eq 30 ]; then
      echo "error: engine not reachable at ${BPM_URL}" >&2
      exit 1
    fi
    sleep 2
  done
fi

# ── Undeploy mode ────────────────────────────────
if [ -n "$UNDEPLOY" ]; then
  echo "Undeploying seed BPMN deployments (source=opensme-bootstrap)…"
  if [ "$USE_DOCKER_EXEC" = true ]; then
    deployments_json="$(docker exec opensme-camunda curl -sf \
      'http://localhost:8080/engine-rest/deployment?source=opensme-bootstrap' 2>/dev/null || echo '{"data":[]}')"
  else
    deployments_json="$(curl -sf "${BPM_URL}/engine-rest/deployment?source=opensme-bootstrap" 2>/dev/null || echo '{"data":[]}')"
  fi

  lines="$(printf '%s' "$deployments_json" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
data = d.get("data") or d.get("result") or []
for dep in data:
    print("%s\t%s" % (dep.get("id",""), dep.get("name","")))
' 2>/dev/null || true)"

  if [ -z "$lines" ]; then
    echo "  no seed deployments found — nothing to undeploy"
    exit 0
  fi

  removed=0
  while IFS=$'\t' read -r id name; do
    [ -n "$id" ] || continue
    if [ "$UNDEPLOY" = "all" ] || [[ "$name" == *"$UNDEPLOY"* ]]; then
      if [ "$USE_DOCKER_EXEC" = true ]; then
        if docker exec opensme-camunda curl -sf -X DELETE \
            "http://localhost:8080/engine-rest/deployment/${id}?cascade=true" >/dev/null 2>&1; then
          echo "  removed ${name} (${id})"
          removed=$((removed + 1))
        fi
      else
        if curl -sf -X DELETE "${BPM_URL}/engine-rest/deployment/${id}?cascade=true" >/dev/null 2>&1; then
          echo "  removed ${name} (${id})"
          removed=$((removed + 1))
        fi
      fi
    fi
  done <<< "$lines"

  echo "Undeployed ${removed} deployment(s)."
  exit 0
fi

# ── Deploy each .bpmn file ─────────────────────────
deployed=0
for bpmn_file in "${BPMN_DIR}"/*.bpmn; do
  [ -f "$bpmn_file" ] || continue
  name="$(basename "$bpmn_file")"
  echo "Deploying ${name}…"

  if [ "$USE_DOCKER_EXEC" = true ]; then
    docker cp "$bpmn_file" "opensme-camunda:/tmp/${name}" >/dev/null
    response=$(docker exec opensme-camunda curl -sf -X POST \
      "http://localhost:8080/engine-rest/deployment/create" \
      -H "Accept: application/json" \
      -F "deployment-name=${name}" \
      -F "deployment-source=opensme-bootstrap" \
      -F "deploy-changed-only=false" \
      -F "${name}=@/tmp/${name};type=text/xml") || {
      echo "  error: deployment failed for ${name}" >&2
      docker exec opensme-camunda rm -f "/tmp/${name}" 2>/dev/null || true
      exit 1
    }
    docker exec opensme-camunda rm -f "/tmp/${name}" 2>/dev/null || true
  else
    response=$(curl -sf -X POST "${BPM_URL}/engine-rest/deployment/create" \
      -H "Accept: application/json" \
      -F "deployment-name=${name}" \
      -F "deployment-source=opensme-bootstrap" \
      -F "deploy-changed-only=false" \
      -F "${name}=@${bpmn_file};type=text/xml") || {
      echo "  error: deployment failed for ${name}" >&2
      exit 1
    }
  fi

  proc_def=$(echo "$response" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if d.get('deployedProcessDefinitions'):
        for k, v in d['deployedProcessDefinitions'].items():
            print(f'  process: {v.get(\"key\",\"?\")} v{v.get(\"version\",\"?\")} — {v.get(\"name\",\"?\")}')
    else:
        print('  deployed (no process definitions)')
except Exception as e:
    print(f'  deployed (parse error: {e})')
" 2>&1)
  echo "$proc_def"
  deployed=$((deployed + 1))
done

if [ "$deployed" -eq 0 ]; then
  echo "No .bpmn files found in ${BPMN_DIR}"
  exit 0
fi

echo ""
echo "Deployed ${deployed} process definition(s)."
if [ "$USE_DOCKER_EXEC" = true ]; then
  echo "Cockpit: http://localhost:8080/operaton/app/   (default: demo/demo)"
  echo "REST:    http://localhost:8080/engine-rest/engine"
else
  echo "Cockpit: ${BPM_URL}/operaton/app/   (default: demo/demo)"
  echo "REST:    ${BPM_URL}/engine-rest/engine"
fi
