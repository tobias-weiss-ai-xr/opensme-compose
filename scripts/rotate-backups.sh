#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# openSME — Off-site backup rotation via rclone
# ═══════════════════════════════════════════════════════════════
# Prunes old backups from an rclone remote (S3 / MinIO / OpenCloud
# object storage) while keeping the most recent RETENTION days.
# Complements scripts/backup.sh --s3, which uploads backups to the
# remote but never removes anything (old copies accumulate forever).
#
# Usage:
#   ./scripts/rotate-backups.sh --remote s3:opensme-backups
#   ./scripts/rotate-backups.sh --remote s3:opensme-backups --dry-run
#   ./scripts/rotate-backups.sh --remote s3:opensme-backups --retention 7
#   ./scripts/rotate-backups.sh --remote s3:opensme-backups --prefix backups/ --help
#
# Safety:
#   - --remote is required; nothing is ever deleted without it.
#   - The script refuses to prune an empty/root prefix, so the remote
#     root itself can never be targeted.
#   - Only the subtree under --prefix is ever touched.
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

RETENTION=30
REMOTE=""
PREFIX="backups/"
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: ./scripts/rotate-backups.sh [options]

Prune old backups from an rclone remote (off-site object storage),
keeping the last RETENTION days. Complements scripts/backup.sh --s3
(which uploads backups but never rotates them).

Options:
  --remote NAME    rclone remote + bucket (e.g. 's3:opensme-backups'); REQUIRED
  --retention N    keep N days of backups on the remote (default 30)
  --prefix P       only prune paths under this prefix (default: backups/)
  --dry-run        show what would be deleted without deleting anything
  --help           print this help and exit

Examples:
  ./scripts/rotate-backups.sh --remote s3:opensme-backups --dry-run
  ./scripts/rotate-backups.sh --remote s3:opensme-backups --retention 7
  ./scripts/rotate-backups.sh --remote s3:opensme-backups --prefix backups/
EOF
}

# ── Parse arguments ───────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --remote)
      [ $# -lt 2 ] && { echo "Error: --remote requires an argument" >&2; exit 1; }
      REMOTE="$2"; shift 2 ;;
    --retention)
      [ $# -lt 2 ] && { echo "Error: --retention requires an argument" >&2; exit 1; }
      RETENTION="$2"; shift 2 ;;
    --prefix)
      [ $# -lt 2 ] && { echo "Error: --prefix requires an argument" >&2; exit 1; }
      PREFIX="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Error: unknown option: $1" >&2; exit 1 ;;
  esac
done

# --help is handled above, so it works even without rclone installed.
# ── Validate tooling / safety ─────────────────
command -v rclone >/dev/null 2>&1 || {
  echo "Error: rclone is not installed. Install rclone (https://rclone.org) and configure" >&2
  echo "       the remote with 'rclone config' before running rotate-backups.sh." >&2
  exit 1
}

# Refuse to run without a remote target.
[ -n "$REMOTE" ] || {
  echo "Error: --remote NAME is required (e.g. --remote s3:opensme-backups)." >&2
  exit 1
}

# Refuse to operate on the local filesystem: an rclone remote MUST
# contain a colon (remote:path). Without one rclone would treat the
# "remote" as a local directory and delete local files.
case "$REMOTE" in
  *:*) ;;
  *) echo "Error: --remote '${REMOTE}' does not look like an rclone remote (missing ':')." >&2
     exit 1 ;;
esac

# Never delete the root of the remote.
[ -n "$PREFIX" ] || {
  echo "Error: --prefix must not be empty (refusing to prune the remote root)." >&2
  exit 1
}
case "$PREFIX" in
  "/"|"."|"./") echo "Error: --prefix '${PREFIX}' is the root; refusing to delete it." >&2; exit 1 ;;
esac

# Retention must be a positive integer.
case "$RETENTION" in
  ''|*[!0-9]*) echo "Error: --retention must be a positive integer, got '${RETENTION}'." >&2; exit 1 ;;
  '0') echo "Error: --retention must be >= 1 (0 would delete everything)." >&2; exit 1 ;;
esac

# ── Prune ─────────────────────────────────────
TARGET="${REMOTE}/${PREFIX}"

RCLONE_ARGS=( delete --rmdirs --min-age "${RETENTION}d" )
[ "$DRY_RUN" = true ] && RCLONE_ARGS+=( --dry-run )

echo "openSME Rotation"
echo "   Remote:      ${TARGET}"
echo "   Retention:   ${RETENTION} day(s)"
echo "   Mode:        $([ "$DRY_RUN" = true ] && echo 'dry run (nothing deleted)' || echo 'live delete')"
echo ""

OUT_FILE="$(mktemp)"
set +e
rclone "${RCLONE_ARGS[@]}" "${TARGET}" >"$OUT_FILE" 2>&1
RC=$?
set -e

if [ $RC -ne 0 ]; then
  echo "Error: rclone delete failed (exit ${RC})." >&2
  cat "$OUT_FILE" >&2
  rm -f "$OUT_FILE"
  exit 1
fi

COUNT="$(wc -l < "$OUT_FILE" | tr -d ' ')"
echo "Summary: ${COUNT} remote item(s) older than ${RETENTION} day(s) under '${TARGET}'"
[ "$DRY_RUN" = true ] && echo "         would be deleted (dry run — nothing was removed)." \
                      || echo "         were deleted; backups newer than ${RETENTION} day(s) were kept."
[ "$COUNT" -gt 0 ] && { echo "Details:"; cat "$OUT_FILE"; }
rm -f "$OUT_FILE"

echo ""
echo "Done."
