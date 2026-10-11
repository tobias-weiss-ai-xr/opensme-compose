#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# openSME — Restore
# ═══════════════════════════════════════════════════════════════
# Restores PostgreSQL and/or Docker volumes from a backup.
#
# Usage:
#   ./scripts/restore.sh <backup-prefix>           # Restore PG + volumes
#   ./scripts/restore.sh <backup-prefix> --pg-only # Restore PostgreSQL only
#   ./scripts/restore.sh <backup-prefix> --volumes-only # Restore volumes only
#   ./scripts/restore.sh --list                    # List available backups
#   ./scripts/restore.sh --dry-run <backup-prefix> # Preview
#
# Backup files follow the pattern:
#   postgres_<timestamp>.sql.gz    (PostgreSQL dump)
#   traefik_<timestamp>.tar.gz     (Traefik ACME/SSL)
#   volumes_<timestamp>.tar.gz     (Combined volume backup)
#
# The <backup-prefix> is the timestamp: e.g., 20250815_143022
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

BACKUP_DIR="${BACKUP_DIR:-./backups}"

# ── Parse arguments ───────────────────────────
LIST=false
DRY_RUN=false
PG_ONLY=false
VOLUMES_ONLY=false
BACKUP_PREFIX=""

while [ $# -gt 0 ]; do
  case "$1" in
    --list)        LIST=true; shift ;;
    --dry-run)     DRY_RUN=true; shift ;;
    --pg-only)     PG_ONLY=true; shift ;;
    --volumes-only) VOLUMES_ONLY=true; shift ;;
    -h|--help)
      echo "Usage: $0 [--list] [--dry-run] [--pg-only] [--volumes-only] <backup-prefix>"
      echo ""
      echo "Options:"
      echo "  --list           List available backups"
      echo "  --dry-run        Preview without making changes"
      echo "  --pg-only        Restore PostgreSQL only"
      echo "  --volumes-only   Restore Docker volumes only"
      echo "  <backup-prefix>  Timestamp (e.g., 20250815_143022)"
      exit 0
      ;;
    *) BACKUP_PREFIX="$1"; shift ;;
  esac
done

# ── List mode ─────────────────────────────────
if [ "$LIST" = true ]; then
  echo "Available backups in ${BACKUP_DIR}/:"
  echo ""
  # Group by timestamp
  for ts in $(find "${BACKUP_DIR}" -maxdepth 1 -name '*_*.sql.gz' 2>/dev/null | sed 's/.*postgres_//' | sed 's/\.sql\.gz//' | sort -u); do
    echo "  📦 ${ts}"
    [ -f "${BACKUP_DIR}/postgres_${ts}.sql.gz" ] && echo "    PostgreSQL: postgres_${ts}.sql.gz ($(du -h "${BACKUP_DIR}/postgres_${ts}.sql.gz" | cut -f1))"
    [ -f "${BACKUP_DIR}/traefik_${ts}.tar.gz" ] && echo "    Traefik:    traefik_${ts}.tar.gz ($(du -h "${BACKUP_DIR}/traefik_${ts}.tar.gz" | cut -f1))"
    [ -f "${BACKUP_DIR}/volumes_${ts}.tar.gz" ] && echo "    Volumes:    volumes_${ts}.tar.gz ($(du -h "${BACKUP_DIR}/volumes_${ts}.tar.gz" | cut -f1))"
    echo ""
  done
  exit 0
fi

# ── Validate ──────────────────────────────────
if [ -z "$BACKUP_PREFIX" ]; then
  echo "Error: No backup prefix specified"
  echo "Usage: $0 [--pg-only|--volumes-only] <backup-prefix>"
  echo "Run '$0 --list' to see available backups"
  exit 1
fi

PG_FILE="${BACKUP_DIR}/postgres_${BACKUP_PREFIX}.sql.gz"
VOLUMES_FILE="${BACKUP_DIR}/volumes_${BACKUP_PREFIX}.tar.gz"
TRAEFIK_FILE="${BACKUP_DIR}/traefik_${BACKUP_PREFIX}.tar.gz"

echo "📦 openSME Restore"
echo "   Prefix: ${BACKUP_PREFIX}"
echo ""

# ── Dry run ───────────────────────────────────
if [ "$DRY_RUN" = true ]; then
  echo "DRY RUN — What would be restored:"
  if [ "$VOLUMES_ONLY" = false ] && [ -f "$PG_FILE" ]; then
    echo "  PostgreSQL: ${PG_FILE} ($(du -h "$PG_FILE" | cut -f1))"
  elif [ "$VOLUMES_ONLY" = false ]; then
    echo "  ⚠ PostgreSQL backup not found: ${PG_FILE}"
  fi
  if [ "$PG_ONLY" = false ] && [ -f "$VOLUMES_FILE" ]; then
    echo "  Volumes:    ${VOLUMES_FILE} ($(du -h "$VOLUMES_FILE" | cut -f1))"
  elif [ "$PG_ONLY" = false ]; then
    echo "  ⚠ Volumes backup not found: ${VOLUMES_FILE}"
  fi
  if [ "$PG_ONLY" = false ] && [ -f "$TRAEFIK_FILE" ]; then
    echo "  Traefik:    ${TRAEFIK_FILE} ($(du -h "$TRAEFIK_FILE" | cut -f1))"
  elif [ "$PG_ONLY" = false ]; then
    echo "  ⚠ Traefik backup not found: ${TRAEFIK_FILE}"
  fi
  exit 0
fi

# ── Confirm ───────────────────────────────────
echo "⚠ WARNING: This will OVERWRITE existing data!"
echo ""
read -rp "Continue with restore? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
  echo "Restore cancelled."
  exit 0
fi

# ── Stop services ─────────────────────────────
echo "🛑 Stopping services..."
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml}"
export COMPOSE_FILE
docker compose down --remove-orphans 2>/dev/null || true
sleep 5

# ── Restore PostgreSQL ────────────────────────
if [ "$VOLUMES_ONLY" = false ] && [ -f "$PG_FILE" ]; then
  echo "   → Restoring PostgreSQL..."
  # Start just postgres
  docker compose up -d postgres 2>/dev/null || true
  # Wait for readiness — a blind sleep restores into a server that is
  # still booting and silently drops the whole dump.
  for i in $(seq 1 30); do
    docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-opensme}" \
      >/dev/null 2>&1 && break
    [ "$i" = 30 ] && echo "   ⚠ postgres never became ready — aborting restore" >&2 && exit 1
    sleep 2
  done

  # Restore (stderr stays visible — a failed restore must be loud).
  # NO ON_ERROR_STOP here: pg_dumpall emits bare `CREATE ROLE ...` — on a
  # re-restore those legitimately fail with "already exists" and must not
  # abort the remaining databases. Whether the restore actually WORKED is
  # data, not exit codes — the e2e restore drill verifies a marker row.
  gunzip -c "$PG_FILE" | docker compose exec -T postgres \
    psql -U "${POSTGRES_USER:-opensme}" -d postgres \
    || echo "   ⚠ PostgreSQL restore reported errors (check above)"

  echo "   ✓ PostgreSQL restored"
elif [ "$VOLUMES_ONLY" = false ]; then
  echo "   ⚠ PostgreSQL backup not found: ${PG_FILE}"
fi

# ── Restore volumes ───────────────────────────
if [ "$PG_ONLY" = false ] && [ -f "$VOLUMES_FILE" ]; then
  echo "   → Restoring Docker volumes..."
  PROJECT_NAME="${COMPOSE_PROJECT_NAME:-opensme-compose}"
  TEMP_DIR="${BACKUP_DIR}/restore-temp-${BACKUP_PREFIX}"

  mkdir -p "$TEMP_DIR"
  # The combined archive must be a valid outer tar — a corrupt or
  # wrong-format backup must fail loudly here, not pretend to restore.
  if ! tar -xzf "$VOLUMES_FILE" -C "$TEMP_DIR"; then
    echo "   ✗ Combined volume archive is not a valid tar: $VOLUMES_FILE" >&2
    rm -rf "$TEMP_DIR"
    exit 1
  fi

  # Extract individual volume archives
  restored=0
  for vol_archive in "$TEMP_DIR"/*.tar.gz; do
    [ -f "$vol_archive" ] || continue
    restored=$((restored + 1))
    vol_name=$(basename "$vol_archive" .tar.gz)
    full_vol="${PROJECT_NAME}_${vol_name}"

    echo "     → $vol_name..."
    docker volume create "$full_vol" 2>/dev/null || true
    docker run --rm \
      -v "${full_vol}:/data" \
      -v "${TEMP_DIR}:/backups" \
      alpine:3.20 \
      tar xzf "/backups/$(basename "$vol_archive")" -C /data 2>/dev/null \
      || echo "     ⚠ Volume $vol_name restore failed"
  done

  rm -rf "$TEMP_DIR"
  if [ "$restored" -eq 0 ]; then
    echo "   ✗ No per-volume archives inside $VOLUMES_FILE — nothing restored" >&2
    exit 1
  fi
  echo "   ✓ Volumes restored (${restored} volume(s))"
elif [ "$PG_ONLY" = false ]; then
  echo "   ⚠ Volumes backup not found: ${VOLUMES_FILE}"
fi

# ── Restore Traefik (ACME/SSL) ───────────────
# backup.sh tars /etc/traefik from the RUNNING traefik container into
# traefik_<ts>.tar.gz; without this step that ACME/TLS material could never
# come back. Mirror the volumes flow: validate + extract to TEMP_DIR, push
# the files into the traefik-data volume via a throwaway alpine container,
# then bring traefik back so the ACME state is live again. Runs whenever we
# are NOT --pg-only (the ACME/TLS material is volume-like, not SQL).
if [ "$PG_ONLY" = false ] && [ -f "$TRAEFIK_FILE" ]; then
  echo "   → Restoring Traefik (ACME/SSL)..."
  PROJECT_NAME="${COMPOSE_PROJECT_NAME:-opensme-compose}"
  TEMP_DIR="${BACKUP_DIR}/restore-temp-${BACKUP_PREFIX}"

  mkdir -p "$TEMP_DIR"
  # The archive must be a valid tar — a corrupt or wrong-format backup must
  # fail loudly here, not silently leave the ACME state half-restored.
  if ! tar -xzf "$TRAEFIK_FILE" -C "$TEMP_DIR"; then
    echo "   ✗ Traefik archive is not a valid tar: $TRAEFIK_FILE" >&2
    rm -rf "$TEMP_DIR"
    exit 1
  fi

  # Push the /etc/traefik contents into the traefik-data volume. The volume
  # is cleared first so a re-restore cannot resurrect stale ACME files
  # (certs for domains removed since the backup). A failed write is loud.
  full_vol="${PROJECT_NAME}_traefik-data"
  docker volume create "$full_vol" 2>/dev/null || true
  if docker run --rm \
      -v "${full_vol}:/data" \
      -v "${TEMP_DIR}:/backups" \
      alpine:3.20 \
      sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null; cp -a /backups/. /data/'; then
    rm -rf "$TEMP_DIR"
    echo "   ✓ Traefik restored"
  else
    rm -rf "$TEMP_DIR"
    echo "   ✗ Traefik restore failed (ACME state NOT written)" >&2
    exit 1
  fi

  # Bring traefik back so the ACME state is live again
  docker compose up -d traefik 2>/dev/null || true
  echo "   ✓ Traefik restarted"
else
  echo "   ⚠ Traefik backup not found: ${TRAEFIK_FILE}"
fi

# ── Restart services ──────────────────────────
echo "🚀 Restarting services..."
docker compose up -d
sleep 15

echo ""
echo "✅ Restore complete!"
echo "   Check service health: docker compose ps"
echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restore completed: ${BACKUP_PREFIX}" >> "${BACKUP_DIR}/restore.log"
