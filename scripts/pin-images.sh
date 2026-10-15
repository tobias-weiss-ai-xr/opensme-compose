#!/usr/bin/env bash
# ── openSME — Digest-pinned image manifest ──────────────────────────
# Operator opt-in tool for reproducible deploys.
#
#   scripts/pin-images.sh --help      print this usage text
#   scripts/pin-images.sh --generate  resolve every image tag in the compose
#                                     files to its current sha256 digest and
#                                     write manifests/pinned-images.env
#   scripts/pin-images.sh --check     validate manifests/pinned-images.env
#                                     (NAME=repo@sha256:hex per line)
#
# The script NEVER edits a compose file. It only produces/validates the
# digest manifest. `docker compose` consumes it as an env override file:
#
#   docker compose --env-file manifests/pinned-images.env up -d
#
# Requires (for --generate): `docker` (with `buildx imagetools`) or
# `skopeo`; --help and --check work without docker or network access.
set -euo pipefail

NL=$'\n'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MANIFEST="${PINNED_MANIFEST:-$REPO_ROOT/manifests/pinned-images.env}"
# Scan order is fixed so the output is deterministic across runs.
COMPOSE_FILES=(docker-compose.yml services/*.yml profiles/*.yml)
RESOLVE_TIMEOUT="${PINNED_RESOLVE_TIMEOUT:-30}"   # seconds per image

usage() {
  cat <<'EOF'
── openSME — Digest-pinned image manifest ──────────────────────────
Operator opt-in tool for reproducible deploys.

  scripts/pin-images.sh --help      print this usage text
  scripts/pin-images.sh --generate  resolve every image tag in the compose
                                    files to its current sha256 digest and
                                    write manifests/pinned-images.env
  scripts/pin-images.sh --check     validate manifests/pinned-images.env
                                    (NAME=repo@sha256:hex per line)

The script NEVER edits a compose file. It only produces/validates the
digest manifest. `docker compose` consumes it as an env override file:

  docker compose --env-file manifests/pinned-images.env up -d

Requires (for --generate): `docker` (with `buildx imagetools`) or
`skopeo`; --help and --check work without docker or network access.
EOF
}

die()  { printf 'pin-images: %s\n' "$*" >&2; exit 1; }
warn() { printf 'pin-images: %s\n' "$*" >&2; }

# ── Compose discovery ───────────────────────────────────────────────
# Emits lines "SERVICE<TAB>IMAGE_VALUE" for every `image:` key found in
# the compose files. A service key is only trusted inside a `services:`
# block at exactly 2-space indent; nested keys (networks/volumes/…)
# never re-scope the parser, so per-service overrides stay correct.
discover_images() {
  (cd "$REPO_ROOT" && awk '
    /^services:[[:space:]]*$/                { section = "services"; next }
    /^(networks|volumes|configs|secrets|profile|x-[A-Za-z0-9_.-]+):([[:space:]]|$)/ {
      section = ""; next
    }
    section == "services" && /^ {2}[A-Za-z0-9_.-]+:[[:space:]]*(#.*)?$/ {
      svc = $0; sub(/^ {2}/, "", svc); sub(/:.*/, "", svc); next
    }
    section == "services" && /^[[:space:]]+image:[[:space:]]*/ {
      val = $0; sub(/^[[:space:]]*image:[[:space:]]*/, "", val)
      sub(/[[:space:]]*#.*$/, "", val)
      printf "%s\t%s\n", svc, val
    }
  ' "${COMPOSE_FILES[@]}")
}

# Expand nested ${VAR} / ${VAR:-default} slices of an image ref to their
# compose-file defaults. Always resolves defaults — never the caller's
# environment — so reruns produce identical output.
expand_defaults() {
  local s="$1" pat inner out
  while [[ "$s" =~ \$\{[A-Za-z_][A-Za-z0-9_]*(\:-[^}]*)?\} ]]; do
    pat="${BASH_REMATCH[0]}"
    inner="${pat#\$\{}"; inner="${inner%\}}"
    if [[ "$inner" == *":-"* ]]; then out="${inner#*:-}"; else out=""; fi
    s="${s//"$pat"/$out}"
  done
  printf '%s\n' "$s"
}

# ── Variable-name derivation for images without an override ─────────
# Strip well-known role suffixes so `paperless-ngx` → PAPERLESS_IMAGE;
# on collision fall back to the full service name. Registers the chosen
# name in USED_VARS and echoes it. Not needed for whole-image overrides
# (image: ${VAR:…}), which reuse the compose-file var name verbatim.
declare -A USED_VARS=()
# derive_var sets this global instead of echoing so that the USED_VARS
# dedup state survives (command substitution would run a subshell and
# throw the registration away).
DERIVED_VAR=""
derive_var() {
  local svc="$1"
  local base="$svc" sfx cand
  for sfx in -ngx -backend -frontend -y-provider -web -cloud-sync \
             -gotenberg -tika -db -data; do
    if [[ "$base" == *"$sfx" ]] && ((${#base} > ${#sfx})); then
      base="${base%$sfx}"; break
    fi
  done
  cand="${base//-/_}"; cand="${cand^^}_IMAGE"
  if [[ -n "${USED_VARS[$cand]:-}" ]]; then
    cand="${svc//-/_}"; cand="${cand^^}_IMAGE"
  fi
  USED_VARS["$cand"]=1
  DERIVED_VAR="$cand"
}

# ── Digest resolution ───────────────────────────────────────────────
# Echoes "sha256:<64 hex>" for the manifest/index digest of a tag.
# Chain: docker buildx imagetools inspect (plain-text digest) →
# skopeo inspect (parses its .Digest field). Empty on failure.
TO() { if command -v timeout >/dev/null 2>&1; then timeout "$RESOLVE_TIMEOUT" "$@"; else "$@"; fi; }

resolve_digest() {
  local ref="$1" out=""
  if command -v docker >/dev/null 2>&1; then
    out="$(TO docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "$ref" 2>/dev/null | head -n1 || true)"
    out="${out//$'\r'/}"
    if [[ "$out" =~ ^sha256:[0-9a-f]{64}$ ]]; then printf '%s\n' "$out"; return 0; fi
  fi
  if command -v skopeo >/dev/null 2>&1; then
    out="$(TO skopeo inspect "docker://$ref" 2>/dev/null | \
             grep -m1 -E '^[[:space:]]*"Digest"[[:space:]]*:[[:space:]]*"sha256:[0-9a-f]{64}"' | \
             sed -E 's/.*"sha256:([0-9a-f]{64})".*/sha256:\1/' || true)"
    if [[ "$out" =~ ^sha256:[0-9a-f]{64}$ ]]; then printf '%s\n' "$out"; return 0; fi
  fi
  return 1
}

need_engine() {
  command -v docker >/dev/null 2>&1 || command -v skopeo >/dev/null 2>&1 \
    || die "no image tool found (need 'docker manifest inspect' via docker \
buildx, or 'skopeo inspect'); install one and retry"
}

# ── generate ────────────────────────────────────────────────────────
cmd_generate() {
  need_engine
  local content="" active=0 commented=0 unresolved=0 total=0
  local svc img var override bare digest ref line

  content+="# openSME — digest-pinned image manifest (operator opt-in)${NL}"
  content+="# Generated by scripts/pin-images.sh --generate — do not edit by hand.${NL}"
  content+="# Docs: docs/pin-images.md${NL}"
  content+="#${NL}"
  content+="# Feed this file to docker compose as env-file overrides:${NL}"
  content+="#   docker compose --env-file manifests/pinned-images.env up -d${NL}"
  content+="#${NL}"
  content+="# Every image tag (including :latest) is pinned to the sha256 digest${NL}"
  content+="# that was current at generation time — re-run --generate to refresh.${NL}"
  content+="# Commented lines have NO \${VAR} override in the compose files yet;${NL}"
  content+="# wire the override there to activate the pin.${NL}"

  while IFS=$'\t' read -r svc img; do
    [[ -z "$img" ]] && continue
    img="${img%\"}"; img="${img#\"}"; img="${img%\'}"; img="${img#\'}"

    if [[ "$img" =~ ^\$\{([A-Za-z_][A-Za-z0-9_]*)(:-(.*))?\}$ ]]; then
      # Whole-image override: reuse the compose-file var name verbatim.
      var="${BASH_REMATCH[1]}"
      [[ -n "${USED_VARS[$var]:-}" ]] && continue
      USED_VARS["$var"]=1
      override=1
      bare="$(expand_defaults "${BASH_REMATCH[3]:-}")"
      bare="${bare//[[:space:]]/}"
      if [[ -z "$bare" ]]; then
        warn "$img: no default tag in compose — cannot pin (operator must set $var)"
        continue
      fi
    else
      # Hardcoded (or partially-parameterised) image: note in a comment.
      derive_var "$svc"; var="$DERIVED_VAR"
      override=0
      bare="$(expand_defaults "$img")"
    fi

    if [[ "$bare" == *"@sha256:"* ]]; then
      warn "$img: already digest-pinned — skipping"; continue
    fi

    digest="$(resolve_digest "$bare" || true)"
    if [[ -z "$digest" ]]; then
      unresolved=$((unresolved + 1))
      warn "could not resolve '$bare' (offline, private, or local build) — skipped"
      if ((override)); then
        line="# $var=$bare  # digest could not be resolved at generation time"
      else
        line="# $var=$bare  # no \${$var} override in compose; digest could not be resolved"
      fi
      content+="$line${NL}"
      continue
    fi

    ref="${bare%%:*}"                       # strip :tag — repo@sha256 suffices
    if ((override)); then
      active=$((active + 1))
      content+="${var}=${ref}@${digest}${NL}"
    else
      commented=$((commented + 1))
      content+="# ${var}=$ref@$digest  # no \${${var}} override in compose file yet — wire it to activate${NL}"
    fi
    total=$((total + 1))
  done < <(discover_images)

  if ((total == 0)); then
    die "no digests could be resolved — nothing written; check tool/network/auth"
  fi

  mkdir -p "$(dirname "$MANIFEST")"
  printf '%s' "$content" > "$MANIFEST"
  printf 'pin-images: wrote %s (%d active pins, %d commented, %d unresolved)\n' \
         "$MANIFEST" "$active" "$commented" "$unresolved"
}

# ── check ───────────────────────────────────────────────────────────
cmd_check() {
  if [[ ! -f "$MANIFEST" ]]; then
    die "$MANIFEST not found — no digest pins active (run --generate first)"
  fi
  local bad=0 lineno=0 line=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    line="${line%%$'\r'}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*=[^[:space:]@]+@sha256:[0-9a-fA-F]{64}$ ]]; then
      printf 'pin-images: invalid manifest line %d: %s\n' "$lineno" "$line" >&2
      bad=1
    fi
  done < "$MANIFEST"
  if ((bad)); then
    die "$MANIFEST is INVALID (see lines above)"
  fi
  printf 'pin-images: %s is valid (NAME=repo@sha256:hex per line)\n' "$MANIFEST"
}

# ── dispatch ────────────────────────────────────────────────────────
mode="${1:-}"
case "$mode" in
  ""|--help|-h|help) usage; exit 0 ;;
  --generate|-g)     cmd_generate ;;
  --check|-c)        cmd_check ;;
  *)                 usage >&2; die "unknown mode: $mode (use --help)" ;;
esac
