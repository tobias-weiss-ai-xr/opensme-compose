#!/usr/bin/env bash
# scripts/ci-e2e-step.sh — run an E2E journey so a failure is diagnosable
# from the public API.
#
# Job logs require admin rights on this repository, so a red step otherwise
# shows only "Process completed with exit code 1". This wrapper tees the
# journey output, copies the failure context into the step summary (visible
# on the run page) and turns the failing lines into check-run annotations
# (readable via GET /repos/{owner}/{repo}/check-runs/{id}/annotations).
#
# Usage: bash scripts/ci-e2e-step.sh python3 tests/05-e2e/<journey>.py [args]
set -o pipefail

LOG=${CI_E2E_LOG:-/tmp/ci-e2e.log}
SUMMARY=${GITHUB_STEP_SUMMARY:-/dev/null}

rc=0
"$@" 2>&1 | tee "$LOG" || rc=$?

if [ "$rc" -ne 0 ]; then
  {
    echo "## \`$*\` failed (rc=$rc)"
    echo
    echo '```'
    grep -nE '✗|Traceback|Error|FAIL|error:' "$LOG" | head -40 || true
    echo '```'
    echo
    echo "### last 40 lines"
    echo
    echo '```'
    tail -40 "$LOG"
    echo '```'
  } >> "$SUMMARY"

  # Annotations are capped per step — only the decisive lines are sent.
  grep -E '✗|Traceback|error:' "$LOG" | head -10 \
    | sed 's/%/%25/g' \
    | awk '{printf "::error::%s%%0A\n", $0}' || true
fi

exit "$rc"
