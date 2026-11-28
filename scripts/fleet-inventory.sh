#!/usr/bin/env bash
# fleet-inventory.sh — canonical fleet image inventory and live-subset gate.
# Usage:
#   scripts/fleet-inventory.sh [--live] [--resolve-local]
# Writes docs/sbom/fleet-inventory.tsv (or $FLEET_INVENTORY).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${FLEET_INVENTORY:-$ROOT/docs/sbom/fleet-inventory.tsv}"
LIVE=0
RESOLVE=0
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    --resolve-local) RESOLVE=1 ;;
    --help|-h)
      sed -n '1,12p' "$0"
      exit 0
      ;;
    *)
      echo "fleet-inventory: unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$(dirname "$OUT")"
args=()
if [ "$LIVE" -ne 0 ]; then args+=(--live); fi
if [ "$RESOLVE" -ne 0 ]; then args+=(--resolve-local); fi
python3 "$ROOT/scripts/compose_images.py" "${args[@]}" > "$OUT"

echo "fleet inventory: $OUT"
awk 'NR>1 {print $2}' "$OUT" | sort -u | wc -l | tr -d ' ' | xargs -I{} echo "canonical images: {}"
