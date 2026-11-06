#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Build the NosDesk ticketing image from upstream source.
#
# WHY: the published image (ghcr.io/nosdesk/nosdesk) is amd64-only, so ARM
# (Raspberry Pi) deployments cannot pull it. Upstream's own Dockerfile is
# arch-aware (arm64 pdfium + multi-arch stage bases), so we build it
# ourselves — reproducibly, pinned to an exact upstream tag.
#
# License note: Nosdesk is Business Source License 1.1 (source-available).
# Self-hosting / internal use is permitted (one workspace, no license key);
# see LICENSING.md upstream before offering ticketing as a paid service.
#
# Usage:
#   ./scripts/build-nosdesk.sh                 # native arch, tag nosdesk:<ver>
#   NOSDESK_VERSION=1.0.12 ./scripts/build-nosdesk.sh
#   ./scripts/build-nosdesk.sh --platform linux/amd64,linux/arm64 --push
#       # multi-arch cross-build (needs QEMU binfmt + a registry token);
#       # with --push, NOSDESK_IMAGE_TAG must be a full registry ref.
#
# Env overrides (all optional):
#   NOSDESK_VERSION     upstream git tag to build (default 1.0.12 — what the
#                       repo pins in services/ticketing.yml)
#   NOSDESK_IMAGE_TAG   image tag(s) to produce (default nosdesk:<ver>)
#   NOSDESK_PLATFORMS   buildx --platform value (default: native host arch)
#   NOSDESK_SRC_DIR     existing upstream checkout (monorepo ROOT: backend/,
#                       frontend/, i18n/, packages/, ...) instead of cloning
#   NOSDESK_REMOTE      upstream git URL
#   NOSDESK_BUILDER     buildx builder name (e.g. a docker-container builder
#                       created for multi-arch; default = active builder)
#
# After building: point NOSDESK_IMAGE=<tag> in .env to stop pulling the
# amd64-only ghcr release (see .env.example comment).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

ver="${NOSDESK_VERSION:-1.0.12}"
tag="${NOSDESK_IMAGE_TAG:-nosdesk:${ver}}"
remote="${NOSDESK_REMOTE:-https://github.com/Nosdesk/Nosdesk.git}"
platforms="${NOSDESK_PLATFORMS:-}"
builder="${NOSDESK_BUILDER:-}"
push=0

usage() {
  cat <<'EOF'
Build the NosDesk ticketing image from upstream source (arch-agnostic).

Usage:
  ./scripts/build-nosdesk.sh                  native arch, tag nosdesk:<ver>
  NOSDESK_VERSION=1.0.12 ./scripts/build-nosdesk.sh
  ./scripts/build-nosdesk.sh --platform linux/amd64,linux/arm64 --push

Env overrides (all optional):
  NOSDESK_VERSION   upstream git tag to build (default 1.0.12)
  NOSDESK_IMAGE_TAG image tag(s) to produce (default nosdesk:<ver>)
  NOSDESK_PLATFORMS buildx --platform value (default: native host arch)
  NOSDESK_SRC_DIR   existing upstream checkout (monorepo root) to reuse
  NOSDESK_REMOTE    upstream git URL
  NOSDESK_BUILDER   buildx builder name (default: active builder)

Options:
  --platform <list>  buildx --platform (comma-separated)
  --push             push to the registry named in NOSDESK_IMAGE_TAG
  -h, --help         this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) platforms="$2"; shift 2 ;;
    --push) push=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker not found" >&2
  exit 1
fi

# Native fallback for --platform: ask the daemon which platform it runs.
if [[ -z "$platforms" ]]; then
  platforms="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null || echo linux/amd64)"
fi

# Resolve build context: honor an existing checkout, else shallow-clone the tag.
src="${NOSDESK_SRC_DIR:-}"
if [[ -n "$src" ]]; then
  echo "Using existing checkout: $src"
else
  src="$(mktemp -d "${TMPDIR:-/tmp}/nosdesk-build.XXXXXX")"
  echo "Cloning $remote @ v${ver} (shallow)..."
  git clone --quiet --depth 1 --branch "v${ver}" "$remote" "$src"
fi
cd "$src"
head_sha="$(git rev-parse --short HEAD)"
printf 'Building NosDesk v%s (commit %s) for [%s] -> %s\n' \
  "$ver" "$head_sha" "$platforms" "$tag"

build_flags=(-f backend/Dockerfile --target runtime)
# Bake the version into the binary the same way upstream CI does.
build_flags+=(--build-arg "NOSDESK_VERSION=${ver}")
build_flags+=(--build-arg "VITE_BUILD_SHA=${head_sha}")
build_flags+=(--platform "$platforms")

if [[ -n "$builder" ]]; then
  build_flags+=(--builder "$builder")
fi

if [[ "$push" -eq 1 ]]; then
  build_flags+=(--push)
else
  # --load works only for a single platform; warn when asked for many.
  if [[ "$platforms" == *,* ]]; then
    echo "warning: multiple platforms requested without --push; --load only" >&2
    echo "         works for a single platform. Use --push (registry) or" >&2
    echo "         build one platform at a time." >&2
  fi
  build_flags+=(--load)
fi

docker buildx build "${build_flags[@]}" -t "$tag" .

echo
echo "Built: $tag"
echo "Wire it into openSME:  NOSDESK_IMAGE=$tag  (in .env, see .env.example)"
echo "(repeat builds are layer-cached by buildx/cargo-chef, so they are fast)"
