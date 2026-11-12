#!/usr/bin/env bash
# ── CI wrapper around scripts/demo.sh ─────────────────────────────
# Job logs are only downloadable with admin rights, so a failing boot used to
# surface as a bare "Process completed with exit code 1". This wrapper keeps
# the full output, and on failure emits the tail as a check-run annotation —
# readable through the public API and in the PR UI without a login.
#
# Usage: bash scripts/ci-boot-demo.sh [demo.sh args...]
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log="$(mktemp)"
trap 'rm -f "$log"' EXIT

bash "$here/demo.sh" "$@" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}

if (( rc != 0 )); then
  # Annotations are single-line and capped; join the tail with %0A and
  # escape the % that introduces the escape itself.
  tail_msg=$(tail -n 15 "$log" \
    | sed -e 's/%/%25/g' -e 's/\r//g' -e 's/\x1b\[[0-9;]*m//g' \
    | awk '{printf "%s%%0A", $0}')
  echo "::error title=demo.sh failed (rc=$rc)::${tail_msg:0:900}"

  echo "::group::boot diagnostics"
  docker info --format 'server={{.ServerVersion}} driver={{.Driver}} root={{.DockerRootDir}}' 2>&1 || true
  df -h / | tail -1
  docker ps -a --format '{{.Names}}\t{{.Status}}' | head -20 || true
  docker compose -f docker-compose.yml -f idm/zitadel.yml \
    -f opencloud/opencloud.yml -f profiles/demo.dev.yml \
    --profile standalone ps 2>&1 | head -20 || true
  echo "::endgroup::"
fi

exit "$rc"
