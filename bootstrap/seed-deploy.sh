#!/usr/bin/env bash
# ── openSME — Seed content provisioning (Paperless) ──
# Imports bootstrap/seed-content/*.md into Paperless so a fresh stack ships
# with meaningful content, not just identity rows.
#
# Transport: documents are dropped into the container's internal consume
# directory (PAPERLESS_CONSUMPTION_DIR — the /consume compose bind is
# read-only) via `docker cp`. Paperless only consumes files with SUPPORTED
# extensions (.md is not one of them), so the seed markdown is renamed to a
# `.txt` target — the title (basename without extension) stays unchanged and
# the content remains full plain text. The document consumer then imports it
# through the normal production pipeline. Title checks, seed-tag attachment
# and deletions run server-side via the container's manage.py — no API token
# is created or exposed, so the operation works with zero standing
# credentials.
#
# Idempotent: documents whose exact title already exists are skipped;
# `--delete` removes exactly what was seeded (counter-proof).
#
# Usage:
#   ./bootstrap/seed-deploy.sh                       # import seed docs
#   ./bootstrap/seed-deploy.sh --delete              # remove seeded docs
#   ./bootstrap/seed-deploy.sh --dry-run             # show the plan
#   ./bootstrap/seed-deploy.sh --help, -h
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[SEED]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC}  $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

SEED_DIR="$ROOT/bootstrap/seed-content"
CONTAINER="opensme-paperless"
SEED_TAG="Seed"
CONSUME_DIR=""
DELETE=false
DRY_RUN=false

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --delete)  DELETE=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *)         err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

# Resolve the container's consumption dir (guarded — local repo may have no
# docker daemon / container; --help and --dry-run must still work).
CONSUME_DIR="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" 2>/dev/null \
  | sed -n 's/^PAPERLESS_CONSUMPTION_DIR=//p' | head -1 || true)"
[[ -z "$CONSUME_DIR" ]] && CONSUME_DIR="/usr/src/paperless/consume"

# ── guards ────────────────────────────────────────────────────────────────
if [[ ! -d "$SEED_DIR" ]]; then
  err "seed-content dir $SEED_DIR missing"
  exit 1
fi
# Only the documented content documents — README.md is the editorial contract.
mapfile -t FILES < <(find "$SEED_DIR" -maxdepth 1 -name '*.md' ! -name 'README.md' | sort || true)
if [[ ${#FILES[@]} -eq 0 ]]; then
  warn "no seed documents (only README) — nothing to do"
  exit 0
fi
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  warn "paperless container '$CONTAINER' is not running — seed rollout skipped"
  exit 0
fi

title_of() { basename "$1" .md; }
titles_newline() { local f; for f in "${FILES[@]}"; do title_of "$f"; done; }

# Run a Django snippet inside the container; titles are fed on stdin (pass -i).
pshell() { docker exec -i "$CONTAINER" ./manage.py shell -c "$1"; }

# One line "YES <title>" / "NO <title>" per wanted title.
check_existing() {
  titles_newline | pshell '
import sys
from documents.models import Document
titles = [l.strip() for l in sys.stdin if l.strip()]
found = set(Document.objects.filter(title__in=titles).values_list("title", flat=True))
for t in titles:
    print("YES" if t in found else "NO", t)
'
}

import_missing() { # expect: titles that must still be imported
  local t
  for t in "$@"; do
    if docker cp "$SEED_DIR/$t.md" "$CONTAINER:$CONSUME_DIR/$t.txt" >/dev/null 2>&1; then
      info "  dropped '${t}.md' -> consumption folder (as ${t}.txt, paperless-supported)"
    else
      warn "  could not drop '${t}.md'"
    fi
  done
}

tag_present() {
  titles_newline | pshell '
import sys
from documents.models import Document, Tag
tag, _ = Tag.objects.get_or_create(name="Seed")
titles = [l.strip() for l in sys.stdin if l.strip() and not l.startswith(("objects imported", "Use "))]
q = Document.objects.filter(title__in=titles)
for d in q:
    d.tags.add(tag)
print("tagged", q.count())
'
}

# ── plan ──────────────────────────────────────────────────────────────────
info "Seed content -> Paperless ($CONTAINER @ $CONSUME_DIR)"
info "  documents: ${FILES[*]##*/}"
if [[ "$DRY_RUN" == true ]]; then
  ok "dry-run — nothing imported/deleted"
  exit 0
fi

# ── --delete: remove exactly the seeded documents ─────────────────────────
if [[ "$DELETE" == true ]]; then
  info "Removing seeded documents…"
  removed="$(titles_newline | pshell '
import sys
from documents.models import Document
titles = [l.strip() for l in sys.stdin if l.strip()]
n, _ = Document.objects.filter(title__in=titles).delete()
print("removed", n)
')"
  ok "  documents removed:$(echo "$removed" | tail -1 | sed 's/removed //')"
  exit 0
fi

# ── import (idempotent by exact title) ────────────────────────────────────
imported=0; skipped=0
missing=()
while read -r yn t; do
  [[ -z "$t" ]] && continue
  case "$yn" in
    YES) ok "  '${t}' already present — skipped"; skipped=$((skipped+1)) ;;
    *)   missing+=("$t") ;;
  esac
done < <(check_existing)

if [[ ${#missing[@]} -gt 0 ]]; then
  import_missing "${missing[@]}"
  info "Waiting for the document consumer (poll by title, max 150s)…"
  for _ in $(seq 1 50); do
    still=()
    while read -r yn t; do
      [[ -z "$t" ]] && continue
      if [[ "$yn" == YES ]]; then
        ok "  '${t}' imported"
        imported=$((imported+1))
      else
        still+=("$t")
      fi
    done < <(printf '%s\n' "${missing[@]}" | pshell '
import sys
from documents.models import Document
titles = [l.strip() for l in sys.stdin if l.strip() and not l.startswith(("objects imported", "Use "))]
found = set(Document.objects.filter(title__in=titles).values_list("title", flat=True))
for t in titles:
    print("YES" if t in found else "NO", t)
' | grep -E "^(YES|NO) ")
    missing=("${still[@]}")
    [[ ${#missing[@]} -eq 0 ]] && break
    sleep 3
  done
fi

# Attach the seed tag to whatever is present (server-side, idempotent).
tagged="$(tag_present | tail -1)"
[[ -n "$tagged" ]] && ok "  $tagged with '$SEED_TAG'"

echo ""
ok "$imported imported, $skipped already present (tag: '$SEED_TAG')"
if [[ ${#missing[@]} -gt 0 ]]; then
  warn "  still missing: ${missing[*]} — consumer busy? re-run to resume (idempotent)"
  exit 1
fi
info "counter-proof: ./bootstrap/seed-deploy.sh --delete"
