#!/usr/bin/env bash
# generate-sbom.sh — generate per-image CycloneDX + SPDX SBOMs and an aggregate.
#
# Usage:
#   scripts/generate-sbom.sh [--inventory FILE] [--images REF[,REF...]]
#                            [--limit N] [--allow-missing]
#
# Outputs under docs/sbom/ (SBOM_OUT_DIR):
#   <service>-<hash>.cdx.json
#   <service>-<hash>.spdx.json
#   fleet.cdx.json
#
# Requires syft (and PyYAML for the inventory helper).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${SBOM_OUT_DIR:-$ROOT/docs/sbom}"
INVENTORY="${SBOM_INVENTORY:-$ROOT/docs/sbom/fleet-inventory.tsv}"
IMAGES=""
LIMIT=0
ALLOW_MISSING=0
args=("$@")
i=0
while (( i < ${#args[@]} )); do
  arg="${args[i]}"
  case "$arg" in
    --inventory)
      i=$((i+1)); INVENTORY="${args[i]:-$INVENTORY}" ;;
    --images)
      i=$((i+1)); IMAGES="${args[i]:-}" ;;
    --limit)
      i=$((i+1)); LIMIT="${args[i]:-0}" ;;
    --allow-missing)
      ALLOW_MISSING=1 ;;
    --help|-h)
      sed -n '1,14p' "$0"
      exit 0
      ;;
    *)
      echo "generate-sbom: unknown argument: $arg" >&2
      exit 2
      ;;
  esac
  i=$((i+1))
done
if ! command -v syft >/dev/null 2>&1; then
  echo "generate-sbom: syft not found" >&2
  exit 1
fi
if [[ ! -f "$INVENTORY" ]]; then
  echo "generate-sbom: inventory not found: $INVENTORY" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
images=()
if [[ -n "$IMAGES" ]]; then
  IFS=',' read -r -a images <<< "$IMAGES"
else
  while IFS=$'\t' read -r service image _digest _source; do
    [[ "$service" == "service" || -z "$image" ]] && continue
    images+=("$image")
  done < "$INVENTORY"
fi
if (( LIMIT > 0 )); then
  images=("${images[@]:0:LIMIT}")
fi

hash_image() {
  printf '%s' "$1" | sha256sum | cut -c1-12
}

generated=0
failed=0
for image in "${images[@]}"; do
  hash="$(hash_image "$image")"
  safe="$(printf '%s' "$image" | tr -c 'A-Za-z0-9_.-' '_')"
  base="${safe}-${hash}"
  cdx="$OUT_DIR/${base}.cdx.json"
  spdx="$OUT_DIR/${base}.spdx.json"

  if syft "$image" -o cyclonedx-json > "$cdx" && syft "$image" -o spdx-json > "$spdx"; then
    generated=$((generated + 1))
    echo "SBOM: $image -> ${base}.cdx.json / ${base}.spdx.json"
  else
    failed=$((failed + 1))
    rm -f "$cdx" "$spdx"
    echo "SBOM failed: $image" >&2
  fi
done

if (( failed > 0 && ! ALLOW_MISSING )); then
  echo "generate-sbom: $failed image(s) failed; refusing to publish partial set" >&2
  exit 1
fi

python3 "$ROOT/scripts/aggregate_sbom.py" \
  --dir "$OUT_DIR" \
  --inventory "$INVENTORY" \
  --images "${images[@]}" \
  --allow-missing \
  --out "$OUT_DIR/fleet.cdx.json"

echo "SBOM aggregate: $OUT_DIR/fleet.cdx.json ($generated image(s))"
