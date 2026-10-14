#!/usr/bin/env bash
# Layer 0 — Static validation (no containers required).
# Run: bash tests/00-static/run.sh   (or)  make test-static
set -euo pipefail
cd "$(dirname "$0")/../.."

PY="${PYTHON:-python3}"
ok=1
for t in yaml_lint check_env scan_secrets check_artifacts check_perf check_agent check_bootstrap check_backup; do
  echo "── $t ──"
  "$PY" "tests/00-static/$t.py" || ok=0
  echo
done
if [ "$ok" = 1 ]; then
  echo "── ram-budget (dev-maintenance-bot ≤ 128 MB) ──"
  awk -f tests/00-static/sum-memory.awk monitoring/dev-agent.yml || ok=0
  echo
fi
if [ "$ok" = 1 ]; then
  echo "✅ Layer 0 static: ALL PASS"
else
  echo "❌ Layer 0 static: FAILURES (see above)" >&2
  exit 1
fi
