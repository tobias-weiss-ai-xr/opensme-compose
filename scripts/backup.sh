#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# openSME — Enhanced Backup
# ═══════════════════════════════════════════════════════════════
# Backs up:
#   1. PostgreSQL (all databases, via pg_dumpall)
#   2. Traefik ACME/SSL data
#   3. Named Docker volumes (optional)
#
# Usage:
#   ./scripts/backup.sh                         # Full backup (PG + Traefik)
#   ./scripts/backup.sh --volumes              # Also back up Docker volumes
#   ./scripts/backup.sh --volumes --services opencloud,redis  # Specific volumes
#   ./scripts/backup.sh --dry-run              # Preview what would be backed up
#   ./scripts/backup.sh --no-stop              # Don't stop services (risk: inconsistent)
#   ./scripts/backup.sh --encrypt              # AES-256-encrypt every artifact
#   ./scripts/backup.sh --s3                   # Sync backups to an S3 bucket
#                                             #   (MinIO/OpenCloud-compatible)
#   ./scripts/backup.sh --s3-rclone           # Same, via rclone (no s3cmd needed)
#   ./scripts/backup.sh --s3-tool rclone      # Explicit tool: s3cmd|rclone
#
# --encrypt wraps each artifact in AES-256-CBC (openssl, PBKDF2). The
#   passphrase comes from $BACKUP_PASSPHRASE (never a flag; no default).
# --s3 uploads $BACKUP_DIR to s3://$S3_BUCKET/$S3_PREFIX via s3cmd (default)
#   or rclone (--s3-rclone / --s3-tool rclone). Point $S3_ENDPOINT at a
#   MinIO/OpenCloud S3 endpoint (empty = AWS S3); credentials via
#   $S3_ACCESS_KEY / $S3_SECRET_KEY (or s3cmd's own config). With rclone,
#   uploads go to ${RCLONE_REMOTE:-s3:$S3_BUCKET}/$S3_PREFIX using the same
#   S3_* env translated to inline --s3-* flags.
#
# Volume names (without project prefix):
#   postgres-data, redis-data, opencloud-config, opencloud-data,
#   stalwart-etc, stalwart-data, sogo-config, sogo-data,
#   traefik-data, casdoor-data, zitadel-machinekey,
#   cryptpad-data, cryptpad-blob, synapse-data, notes-data,
#   seaweedfs-data, paperless-data, paperless-media, paperless-export,
#   invoiceninja-db (MariaDB), invoiceninja-public, invoiceninja-storage,
#   limesurvey-uploads (survey file attachments/media)
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# ── Config ────────────────────────────────────
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml}"
export COMPOSE_FILE

BACKUP_DIR="${BACKUP_DIR:-./backups}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
DRY_RUN=false
BACKUP_VOLUMES=false
NO_STOP=false
ENCRYPT=false
S3=false
S3_RCLONE=false
S3_TOOL=""                       # --s3-tool s3cmd|rclone (explicit choice)
S3_UPLOAD_TOOL=""                # resolved uploader: s3cmd (default) or rclone
SERVICES=""

# --encrypt / --s3 config (env, no defaults beyond these)
BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-}"
S3_BUCKET="${S3_BUCKET:-}"
S3_ENDPOINT="${S3_ENDPOINT:-}"          # MinIO/OpenCloud host; empty = AWS
S3_ACCESS_KEY="${S3_ACCESS_KEY:-}"
S3_SECRET_KEY="${S3_SECRET_KEY:-}"
S3_PREFIX="${S3_PREFIX:-backups}"
RCLONE_REMOTE="${RCLONE_REMOTE:-s3:${S3_BUCKET}}"   # rclone remote for --s3-rclone
# openssl reads it via `-pass env:`; export so a non-exported caller var works.
export BACKUP_PASSPHRASE

# ── Known volumes ─────────────────────────────
KNOWN_VOLUMES=(
  postgres-data redis-data portal-data opencloud-config opencloud-data
  opencloud-lib
  stalwart-etc stalwart-data sogo-config sogo-data
  traefik-data casdoor-data zitadel-machinekey
  cryptpad-data cryptpad-blob synapse-data notes-data
  seaweedfs-data paperless-data paperless-media paperless-export
  invoiceninja-db invoiceninja-public invoiceninja-storage
  nosdesk-uploads cms-data store-data limesurvey-uploads
  camunda-data
  dev-agent-state dev-maintenance-bot-state predictive-agent-state
  taskfleet-state
  # Deliberately excluded (re-downloadable model cache, too large to
  # back up): ai-models ollama-models — guarded by
  # tests/00-static/check_backup.py
)

# ── Parse arguments ───────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY_RUN=true; shift ;;
    --volumes)    BACKUP_VOLUMES=true; shift ;;
    --no-stop)    NO_STOP=true; shift ;;
    --encrypt)    ENCRYPT=true; shift ;;
    --s3)         S3=true; shift ;;
    --s3-rclone)  S3=true; S3_RCLONE=true; shift ;;
    --s3-tool)    shift; S3=true; S3_TOOL="$1"; shift ;;
    --services)   shift; SERVICES="$1"; shift ;;
    -h|--help)
      echo "Usage: $0 [--volumes] [--services vol1,vol2,...] [--encrypt] [--s3] [--dry-run] [--no-stop]"
      echo ""
      echo "Options:"
      echo "  --volumes           Also back up Docker volumes"
      echo "  --services <list>   Comma-separated volume names (implies --volumes)"
      echo "  --encrypt           Encrypt every artifact with AES-256 (openssl, PBKDF2)"
      echo "  --s3                Upload backups to an S3 bucket (MinIO/OpenCloud)"
      echo "  --s3-rclone         Upload via rclone instead of s3cmd (implies --s3)"
      echo "  --s3-tool <tool>    Upload tool: s3cmd (default) or rclone (implies --s3)"
      echo "  --dry-run           Preview without making changes"
      echo "  --no-stop           Don't stop services (risk: inconsistent backup)"
      echo ""
      echo "--encrypt needs BACKUP_PASSPHRASE; --s3 needs S3_BUCKET (+ S3_ENDPOINT for"
      echo "MinIO/OpenCloud, credentials via S3_ACCESS_KEY/S3_SECRET_KEY)."
      echo ""
      echo "S3 uploader parity: both s3cmd and rclone are supported (default s3cmd)."
      echo "  * s3cmd   (default): s3cmd sync ./backups/ → s3://\$S3_BUCKET/\$S3_PREFIX/"
      echo "  * rclone  (--s3-rclone or --s3-tool rclone): rclone copyto each artifact as"
      echo "    \$RCLONE_REMOTE/\$S3_PREFIX/ — RCLONE_REMOTE defaults to s3:\$S3_BUCKET."
      echo "    S3_ENDPOINT/S3_ACCESS_KEY/S3_SECRET_KEY are passed to rclone as inline"
      echo "    --s3-* flags, so a named rclone remote is NOT required. Use the same"
      echo "    \$RCLONE_REMOTE here as rotate-backups.sh --remote to keep one config."
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Resolve the S3 uploader once: "--s3-tool" wins, else "--s3-rclone" ⇒ rclone,
# else keep the default s3cmd (unchanged behavior).
select_s3_tool() {
  if [ -n "$S3_TOOL" ]; then
    case "$S3_TOOL" in
      s3cmd|rclone) S3_UPLOAD_TOOL="$S3_TOOL" ;;
      *) echo "✗ --s3-tool must be 's3cmd' or 'rclone' (got '$S3_TOOL')" >&2; exit 1 ;;
    esac
  elif [ "$S3_RCLONE" = true ]; then
    S3_UPLOAD_TOOL="rclone"
  else
    S3_UPLOAD_TOOL="s3cmd"
  fi
}
if [ "$S3" = true ]; then
  select_s3_tool
fi

if [ -n "$SERVICES" ]; then
  BACKUP_VOLUMES=true
fi

mkdir -p "$BACKUP_DIR"

echo "📦 openSME Backup"
echo "   Directory:  ${BACKUP_DIR}/"
echo "   Timestamp:  ${TIMESTAMP}"
echo "   PG dump:    yes"
echo "   Traefik:    yes"
echo "   Volumes:    $([ "$BACKUP_VOLUMES" = true ] && echo 'yes' || echo 'no')"
echo "   Encrypt:    $([ "$ENCRYPT" = true ] && echo 'yes (AES-256)' || echo 'no')"
echo "   S3 upload:  $([ "$S3" = true ] && echo "s3://${S3_BUCKET}/${S3_PREFIX}/ via ${S3_UPLOAD_TOOL}" || echo 'no')"
[ -n "$SERVICES" ] && echo "   Services:   ${SERVICES}"
echo ""

# ── Determine volume list ─────────────────────
if [ -n "$SERVICES" ]; then
  SELECTED_VOLUMES=()
  for vol in $(echo "$SERVICES" | tr ',' ' '); do
    found=false
    for v in "${KNOWN_VOLUMES[@]}"; do
      if [ "$v" = "$vol" ]; then
        SELECTED_VOLUMES+=("$vol")
        found=true
        break
      fi
    done
    if [ "$found" = false ]; then
      echo "⚠ Unknown volume: $vol (available: ${KNOWN_VOLUMES[*]})"
      exit 1
    fi
  done
elif [ "$BACKUP_VOLUMES" = true ]; then
  SELECTED_VOLUMES=("${KNOWN_VOLUMES[@]}")
fi

# ── Dry run ───────────────────────────────────
if [ "$DRY_RUN" = true ]; then
  echo "DRY RUN — What would be backed up:"
  echo "  PostgreSQL:  ${BACKUP_DIR}/postgres_${TIMESTAMP}.sql.gz"
  echo "  Traefik:     ${BACKUP_DIR}/traefik_${TIMESTAMP}.tar.gz"
  if [ "$BACKUP_VOLUMES" = true ]; then
    echo "  Volumes:"
    for vol in "${SELECTED_VOLUMES[@]:-}"; do
      echo "    - $vol"
    done
  fi
  if [ "$ENCRYPT" = true ]; then
    [ -n "$BACKUP_PASSPHRASE" ] || { echo "  ✗ --encrypt needs BACKUP_PASSPHRASE set"; exit 1; }
    echo "  * every .sql.gz/.tar.gz becomes <artifact>.enc (AES-256-CBC, PBKDF2)"
  fi
  if [ "$S3" = true ]; then
    [ -n "$S3_BUCKET" ] || { echo "  ✗ --s3 needs S3_BUCKET set"; exit 1; }
    if [ "$S3_UPLOAD_TOOL" = "rclone" ]; then
      echo "    rclone copyto ${BACKUP_DIR}/<artifact> → ${RCLONE_REMOTE}/${S3_PREFIX}/"
    else
      echo "  S3:          s3cmd sync ${BACKUP_DIR}/ → s3://${S3_BUCKET}/${S3_PREFIX}/"
    fi
  fi
  exit 0
fi

# ── Validate flag preconditions (real run) ─────
if [ "$ENCRYPT" = true ] && [ -z "$BACKUP_PASSPHRASE" ]; then
  echo "✗ --encrypt needs BACKUP_PASSPHRASE set (passphrase, no default)"
  exit 1
fi
if [ "$S3" = true ]; then
  [ -n "$S3_BUCKET" ] || { echo "✗ --s3 needs S3_BUCKET set"; exit 1; }
  if [ "$S3_UPLOAD_TOOL" = "rclone" ]; then
    command -v rclone >/dev/null 2>&1 || { echo "✗ --s3-rclone needs rclone installed"; exit 1; }
  else
    command -v s3cmd >/dev/null 2>&1 || { echo "✗ --s3 needs s3cmd installed"; exit 1; }
  fi
fi

# ── Stop services for consistent backup ───────
# NOTE: we no longer `compose down` before the dumps — pg_dumpall and a tar
# of /etc/traefik are safe against a RUNNING stack, and downing the stack
# first made both dumps silently EMPTY (nothing to connect to). The stack
# is only stopped below, and only for volume backups.
if [ "$BACKUP_VOLUMES" = true ]; then
  echo "🛑 Will stop services for the volume backup (after the dumps)"
fi

# ── PostgreSQL dump ───────────────────────────
echo "   → PostgreSQL..."
# Dump the RUNNING server (exec). A second container from `compose run` has
# no server on its socket — it produced a silently EMPTY dump.
if docker compose ps --services --filter status=running 2>/dev/null \
     | grep -q '^postgres$' \
  && docker compose exec -T postgres pg_dumpall -U "${POSTGRES_USER:-opensme}" 2>/dev/null \
       | gzip > "${BACKUP_DIR}/postgres_${TIMESTAMP}.sql.gz"; then
  :  # dump written
else
  echo "   ⚠ PostgreSQL backup skipped (postgres not running)"
fi

# ── Traefik ACME/SSL ──────────────────────────
echo "   → Traefik (ACME certs)..."
# Stream the archive from the RUNNING traefik container. `docker compose run`
# cannot work here: a second traefik instance would collide with the live
# container's published ports 80/443 (which is why the old approach silently
# produced no artifact).
if docker compose ps --services --filter status=running 2>/dev/null \
     | grep -q '^traefik$' \
  && docker compose exec -T traefik tar czf - -C /etc/traefik . \
       > "${BACKUP_DIR}/traefik_${TIMESTAMP}.tar.gz" 2>/dev/null; then
  :  # archive written
else
  echo "   ⚠ Traefik backup skipped (not running or no volume)"
fi

# ── Volume backup ─────────────────────────────
if [ "$BACKUP_VOLUMES" = true ]; then
  PROJECT_NAME="${COMPOSE_PROJECT_NAME:-opensme-compose}"
  PARTIAL_DIR="${BACKUP_DIR}/partial-${TIMESTAMP}"
  mkdir -p "$PARTIAL_DIR"

  # volumes are only consistent while the writers are down
  echo "🛑 Stopping services for volume backup..."
  docker compose down --remove-orphans 2>/dev/null || true
  sleep 5

  for vol in "${SELECTED_VOLUMES[@]}"; do
    FULL_VOL="${PROJECT_NAME}_${vol}"
    echo "   → Volume: $vol..."
    docker run --rm \
      -v "${FULL_VOL}:/data" \
      -v "${PARTIAL_DIR}:/backups" \
      alpine:3.20 \
      tar czf "/backups/${vol}.tar.gz" -C /data . 2>/dev/null \
      || echo "   ⚠ Volume $vol skipped (not found)"
  done

  # Combine volume backups into ONE outer tar containing the per-volume
  # <vol>.tar.gz members — that is exactly the shape restore.sh expects
  # (extract outer archive → loop over member tarballs → one volume each).
  # Byte-concatenating the gzip members (cat a.tar.gz b.tar.gz) is NOT a
  # valid archive: tar stops at the first member's end-of-archive marker
  # and the remaining volumes are silently dropped.
  if [ -d "$PARTIAL_DIR" ] && ls "$PARTIAL_DIR"/*.tar.gz >/dev/null 2>&1; then
    ( cd "$PARTIAL_DIR" && tar czf "${BACKUP_DIR}/volumes_${TIMESTAMP}.tar.gz" ./*.tar.gz )
    rm -rf "$PARTIAL_DIR"
  fi
fi

# ── Restart services (only if volume backup stopped them) ──
if [ "$BACKUP_VOLUMES" = true ] && [ "$NO_STOP" = false ]; then
  echo "🚀 Restarting services..."
  docker compose up -d
  sleep 10
fi

# ── Encrypt (AES-256-CBC, PBKDF2) ──────────────
if [ "$ENCRYPT" = true ]; then
  echo "🔐 Encrypting artifacts (AES-256-CBC)..."
  for f in "${BACKUP_DIR}"/*"_${TIMESTAMP}".sql.gz "${BACKUP_DIR}"/*"_${TIMESTAMP}".tar.gz; do
    [ -f "$f" ] || continue
    openssl enc -aes-256-cbc -pbkdf2 -iter 100000 -salt \
      -pass env:BACKUP_PASSPHRASE -in "$f" -out "$f.enc"
    rm -f "$f"   # plaintext never left at rest
    echo "   → ${f##*/} → ${f##*/}.enc"
  done
fi

# ── S3 upload (MinIO/OpenCloud via s3cmd or rclone) ──────
if [ "$S3" = true ]; then
  if [ "$S3_UPLOAD_TOOL" = "rclone" ]; then
    # rclone uploader: inline S3 provider flags translated from the SAME
    # S3_* env that s3cmd uses, so no named rclone remote is required.
    # Target: ${RCLONE_REMOTE}/${S3_PREFIX}/ where RCLONE_REMOTE defaults
    # to s3:${S3_BUCKET} — mirror rotate-backups.sh --remote for one-config
    # parity.
    echo "☁  Uploading to ${RCLONE_REMOTE}/${S3_PREFIX}/ via rclone ..."
    RCLONE_ARGS=(copyto)
    if [ -n "$S3_ENDPOINT" ]; then
      RCLONE_ARGS+=(--s3-provider Minio --s3-endpoint "$S3_ENDPOINT")
    else
      RCLONE_ARGS+=(--s3-provider AWS)
    fi
    [ -n "$S3_ACCESS_KEY" ] && RCLONE_ARGS+=(--s3-access-key-id "$S3_ACCESS_KEY")
    [ -n "$S3_SECRET_KEY" ] && RCLONE_ARGS+=(--s3-secret-access-key "$S3_SECRET_KEY")
    # shellcheck disable=SC2068
    set +e
    for f in "${BACKUP_DIR}"/*"_${TIMESTAMP}."*; do
      [ -f "$f" ] || continue
      rclone "${RCLONE_ARGS[@]}" "$f" "${RCLONE_REMOTE}/${S3_PREFIX}/"
      RC=$?
      if [ "$RC" -ne 0 ]; then
        echo "Warning: rclone upload of ${f##*/} failed (exit $RC) — local backup remains" >&2
      fi
    done
    set -e
  else
    echo "☁  Uploading to s3://${S3_BUCKET}/${S3_PREFIX}/ ..."
    S3_ARGS=()
    [ -n "$S3_ACCESS_KEY" ] && S3_ARGS+=(--access_key="$S3_ACCESS_KEY")
    [ -n "$S3_SECRET_KEY" ] && S3_ARGS+=(--secret_key="$S3_SECRET_KEY")
    if [ -n "$S3_ENDPOINT" ]; then
      # Path-style URL on the custom host (MinIO/OpenCloud): s3cmd switches
      # off bucket-in-DNS when --host-bucket equals --host.
      S3_ARGS+=(--host="$S3_ENDPOINT" --host-bucket="$S3_ENDPOINT")
    fi
    # shellcheck disable=SC2068
    set +e
    s3cmd sync "${S3_ARGS[@]}" --no-check-md5 --acl-private \
      "${BACKUP_DIR}/" "s3://${S3_BUCKET}/${S3_PREFIX}/"
    RC=$?
    set -e
    if [ "$RC" -ne 0 ]; then
      echo "Warning: S3 upload failed (exit $RC) — local backup remains" >&2
    fi
  fi
fi

# ── Summary ───────────────────────────────────
echo ""
echo "✅ Backup complete!"
echo "   Directory: ${BACKUP_DIR}/"
ls -lh "${BACKUP_DIR}/"*"_${TIMESTAMP}.*" 2>/dev/null || true

# ── Retention (keep 7 days) ───────────────────
find "$BACKUP_DIR" -name "*_*.sql.gz" -type f -mtime +7 -delete 2>/dev/null || true
find "$BACKUP_DIR" -name "*_*.tar.gz" -type f -mtime +7 -delete 2>/dev/null || true
find "$BACKUP_DIR" -name "*.enc" -type f -mtime +7 -delete 2>/dev/null || true
echo "   Retention: Keeping last 7 days"

# ── Log ───────────────────────────────────────
echo "[$(date +'%Y-%m-%d %H:%M:%S')] Backup completed: ${TIMESTAMP}" >> "${BACKUP_DIR}/backup.log"
