#!/usr/bin/env bash
# scan-sbom.sh — Trivy vulnerability scan for canonical compose images.
#
# Usage:
#   scripts/scan-sbom.sh [--inventory FILE] [--images REF[,REF...]]
#                        [--limit N] [--allow-missing]
#
# Outputs docs/sbom/scan/<safe>-<hash>.trivy.json and a .meta.json sidecar.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${SBOM_SCAN_DIR:-$ROOT/docs/sbom/scan}"
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
      sed -n '1,12p' "$0"
      exit 0
      ;;
    *)
      echo "scan-sbom: unknown argument: $arg" >&2
      exit 2
      ;;
  esac
  i=$((i+1))
done

if ! command -v trivy >/dev/null 2>&1; then
  echo "scan-sbom: trivy not found" >&2
  exit 1
fi
if [[ ! -f "$INVENTORY" ]]; then
  echo "scan-sbom: inventory not found: $INVENTORY" >&2
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

trivy_version="$(trivy --version 2>/dev/null | head -n1 || true)"
failed=0
for image in "${images[@]}"; do
  hash="$(hash_image "$image")"
  safe="$(printf '%s' "$image" | tr -c 'A-Za-z0-9_.-' '_')"
  base="${safe}-${hash}"
  scan="$OUT_DIR/${base}.trivy.json"
  meta="$OUT_DIR/${base}.meta.json"

  if trivy image --format json "$image" > "$scan"; then
    python3 - "$image" "$scan" "$meta" "$trivy_version" <<'PY'
import json, sys
from datetime import datetime, timezone
image, scan, meta, trivy_version = sys.argv[1:]
doc = json.load(open(scan))
meta_doc = {
    "schema": 1,
    "image": image,
    "scanned_at": datetime.now(timezone.utc).isoformat(),
    "trivy_version": trivy_version,
    "source": doc.get("Metadata", {}).get("RepoTags") or [],
}
json.dump(meta_doc, open(meta, "w"), indent=2)
PY
    echo "scan: $image -> ${base}.trivy.json"
  else
    failed=$((failed + 1))
    rm -f "$scan" "$meta"
    echo "scan failed: $image" >&2
  fi
done

if (( failed > 0 && ! ALLOW_MISSING )); then
  echo "scan-sbom: $failed image(s) failed" >&2
  exit 1
fi
echo "scan-sbom: $OUT_DIR ($(( ${#images[@]} - failed )) image(s))"
