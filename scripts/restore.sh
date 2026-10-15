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
#   ./scripts/restore.sh <backup-prefix> --passphrase P   # decrypt .enc backups
#
# Backup files follow the pattern:
#   postgres_<timestamp>.sql.gz    (PostgreSQL dump)
#   traefik_<timestamp>.tar.gz     (Traefik ACME/SSL)
#   volumes_<timestamp>.tar.gz     (Combined volume backup)
#
# Encrypted backups (backup.sh --encrypt) look like
#   postgres_<timestamp>.sql.gz.enc  /  traefik_<timestamp>.tar.gz.enc ...
# and are decrypted on the fly with AES-256-CBC (openssl, PBKDF2) using the
# passphrase given via --passphrase or $BACKUP_PASSPHRASE (the same env
# backup.sh --encrypt reads). Plaintext artifacts are preferred when both
# are present.
#
# The <backup-prefix> is the timestamp: e.g., 20250815_143022
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

BACKUP_DIR="${BACKUP_DIR:-./backups}"

# Passphrase for encrypted backups. backupe.sh --encrypt writes with
# $BACKUP_PASSPHRASE and openssl reads it via `-pass env:` — export so a
# non-exported caller var works and the value never shows in `ps`/history.
PASSPHRASE="${BACKUP_PASSPHRASE:-}"
export BACKUP_PASSPHRASE="$PASSPHRASE"

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
    --passphrase)  shift; PASSPHRASE="$1"; export BACKUP_PASSPHRASE="$PASSPHRASE"; shift ;;
    -h|--help)
      echo "Usage: $0 [--list] [--dry-run] [--pg-only] [--volumes-only] [--passphrase P] <backup-prefix>"
      echo ""
      echo "Options:"
      echo "  --list           List available backups"
      echo "  --dry-run        Preview without making changes"
      echo "  --pg-only        Restore PostgreSQL only"
      echo "  --volumes-only   Restore Docker volumes only"
      echo "  --passphrase P   Passphrase for .enc backups (or \$BACKUP_PASSPHRASE)"
      echo "  <backup-prefix>  Timestamp (e.g., 20250815_143022)"
      exit 0
      ;;
    *) BACKUP_PREFIX="$1"; shift ;;
  esac
done

# ── Resolve an artifact (plaintext preferred, else .enc) ──
# $1 = base path without extension (e.g. "${BACKUP_DIR}/postgres_${TS}").
# Sets ACTUAL_FILE (path of the chosen artifact, or "" if none exists) and
# IS_ENCRYPTED (true if the chosen artifact is a .enc). If only a .enc
# exists and no passphrase was given, abort with exit 1 — an encrypted
# backup can never be restored without it.
resolve_artifact() {
  local base="$1"
  ACTUAL_FILE=""
  IS_ENCRYPTED=false
  if [ -f "${base}.sql.gz" ]; then
    ACTUAL_FILE="${base}.sql.gz"
  elif [ -f "${base}.tar.gz" ]; then
    ACTUAL_FILE="${base}.tar.gz"
  elif [ -f "${base}.sql.gz.enc" ]; then
    ACTUAL_FILE="${base}.sql.gz.enc"
    IS_ENCRYPTED=true
  elif [ -f "${base}.tar.gz.enc" ]; then
    ACTUAL_FILE="${base}.tar.gz.enc"
    IS_ENCRYPTED=true
  fi

  if [ -n "$ACTUAL_FILE" ] && [ "$IS_ENCRYPTED" = true ] && [ -z "$PASSPHRASE" ]; then
    echo "Error: encrypted backup found but no passphrase given: ${ACTUAL_FILE}" >&2
    echo "       --passphrase or \$BACKUP_PASSPHRASE required for encrypted backups" >&2
    exit 1
  fi
}

# ── List mode ─────────────────────────────────
if [ "$LIST" = true ]; then
  echo "Available backups in ${BACKUP_DIR}/:"
  echo ""
  # Group by timestamp — include encrypted (.enc) artifacts alongside plaintext.
  for ts in $(find "${BACKUP_DIR}" -maxdepth 1 -type f \
                \( -name 'postgres_*_*.sql.gz' -o -name 'postgres_*_*.sql.gz.enc' \
                   -o -name 'traefik_*_*.tar.gz' -o -name 'traefik_*_*.tar.gz.enc' \
                   -o -name 'volumes_*_*.tar.gz' -o -name 'volumes_*_*.tar.gz.enc' \) \
                2>/dev/null | grep -o '[0-9]\{8\}_[0-9]\{6\}' | sort -u); do
    echo "  📦 ${ts}"
    for entry in "PostgreSQL:postgres_${ts}.sql.gz" "Traefik:traefik_${ts}.tar.gz" "Volumes:volumes_${ts}.tar.gz"; do
      label="${entry%%:*}"; base="${entry#*:}"
      if [ -f "${BACKUP_DIR}/${base}" ]; then
        echo "    ${label}: ${base} ($(du -h "${BACKUP_DIR}/${base}" | cut -f1))"
      elif [ -f "${BACKUP_DIR}/${base}.enc" ]; then
        echo "    ${label}: ${base}.enc [encrypted] ($(du -h "${BACKUP_DIR}/${base}.enc" | cut -f1))"
      fi
    done
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
PG_BASE="${PG_FILE%.sql.gz}"
VOLUMES_BASE="${VOLUMES_FILE%.tar.gz}"
TRAEFIK_BASE="${TRAEFIK_FILE%.tar.gz}"

echo "📦 openSME Restore"
echo "   Prefix: ${BACKUP_PREFIX}"
echo ""

# ── Dry run ───────────────────────────────────
if [ "$DRY_RUN" = true ]; then
  echo "DRY RUN — What would be restored:"
  if [ "$VOLUMES_ONLY" = false ]; then
    resolve_artifact "$PG_BASE"
    if [ -n "$ACTUAL_FILE" ]; then
      enc_note=""
      [ "$IS_ENCRYPTED" = true ] && enc_note=" [encrypted — would decrypt with openssl AES-256]"
      echo "  PostgreSQL: ${ACTUAL_FILE} ($(du -h "$ACTUAL_FILE" | cut -f1))${enc_note}"
    else
      echo "  ⚠ PostgreSQL backup not found: ${PG_FILE}"
    fi
  fi
  if [ "$PG_ONLY" = false ]; then
    resolve_artifact "$VOLUMES_BASE"
    if [ -n "$ACTUAL_FILE" ]; then
      enc_note=""
      [ "$IS_ENCRYPTED" = true ] && enc_note=" [encrypted — would decrypt with openssl AES-256]"
      echo "  Volumes:    ${ACTUAL_FILE} ($(du -h "$ACTUAL_FILE" | cut -f1))${enc_note}"
    else
      echo "  ⚠ Volumes backup not found: ${VOLUMES_FILE}"
    fi
  fi
  if [ "$PG_ONLY" = false ]; then
    resolve_artifact "$TRAEFIK_BASE"
    if [ -n "$ACTUAL_FILE" ]; then
      enc_note=""
      [ "$IS_ENCRYPTED" = true ] && enc_note=" [encrypted — would decrypt with openssl AES-256]"
      echo "  Traefik:    ${ACTUAL_FILE} ($(du -h "$ACTUAL_FILE" | cut -f1))${enc_note}"
    else
      echo "  ⚠ Traefik backup not found: ${TRAEFIK_FILE}"
    fi
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
if [ "$VOLUMES_ONLY" = false ]; then
  resolve_artifact "$PG_BASE"
  if [ -n "$ACTUAL_FILE" ]; then
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

    if [ "$IS_ENCRYPTED" = true ]; then
      echo "   🔓 Decrypting ${ACTUAL_FILE##*/} (openssl AES-256)..."
      # Decrypt + restore as one stream ('openssl enc -d ... | gunzip -c |
      # psql'). PIPESTATUS lets us fail LOUDLY on a bad decrypt / corrupt
      # stream while still tolerating ordinary psql SQL errors below (a
      # pg_dumpall re-restore legitimately fails bare `CREATE ROLE`, and
      # whether the restore WORKED is data, not exit codes).
      set +e
      openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 -salt \
        -pass env:BACKUP_PASSPHRASE -in "$ACTUAL_FILE" \
        | gunzip -c \
        | docker compose exec -T postgres \
            psql -U "${POSTGRES_USER:-opensme}" -d postgres
      # Capture the whole array in ONE expansion — any later command (even a
      # plain assignment) resets PIPESTATUS, so element-by-element reads fail
      # under `set -u`.
      pipe_status=( "${PIPESTATUS[@]}" )
      set -e
      rc_openssl="${pipe_status[0]}"
      rc_gunzip="${pipe_status[1]}"
      rc_psql="${pipe_status[2]}"
      if [ "$rc_openssl" -ne 0 ] || [ "$rc_gunzip" -ne 0 ]; then
        echo "   ✗ Failed to decrypt ${ACTUAL_FILE##*/} — PostgreSQL restore aborted" >&2
        exit 1
      fi
      if [ "$rc_psql" -ne 0 ]; then
        echo "   ⚠ PostgreSQL restore reported errors (check above)"
      fi
    else
      # Restore (stderr stays visible — a failed restore must be loud).
      # NO ON_ERROR_STOP here: pg_dumpall emits bare `CREATE ROLE ...` — on a
      # re-restore those legitimately fail with "already exists" and must not
      # abort the remaining databases. Whether the restore actually WORKED is
      # data, not exit codes — the e2e restore drill verifies a marker row.
      gunzip -c "$ACTUAL_FILE" | docker compose exec -T postgres \
        psql -U "${POSTGRES_USER:-opensme}" -d postgres \
        || echo "   ⚠ PostgreSQL restore reported errors (check above)"
    fi

    echo "   ✓ PostgreSQL restored"
  else
    echo "   ⚠ PostgreSQL backup not found: ${PG_FILE}"
  fi
fi

# ── Restore volumes ───────────────────────────
if [ "$PG_ONLY" = false ]; then
  resolve_artifact "$VOLUMES_BASE"
  if [ -n "$ACTUAL_FILE" ]; then
    echo "   → Restoring Docker volumes..."
    PROJECT_NAME="${COMPOSE_PROJECT_NAME:-opensme-compose}"
    TEMP_DIR="${BACKUP_DIR}/restore-temp-${BACKUP_PREFIX}"
    # Decrypted plaintext goes into its own dir so the per-volume loop below
    # (which scans TEMP_DIR top-level *.tar.gz) never mistakes it for a volume.
    DECRYPT_DIR="${BACKUP_DIR}/decrypt-temp-${BACKUP_PREFIX}"

    mkdir -p "$TEMP_DIR" "$DECRYPT_DIR"
    ARCHIVE="$ACTUAL_FILE"
    if [ "$IS_ENCRYPTED" = true ]; then
      echo "   🔓 Decrypting ${ACTUAL_FILE##*/} (openssl AES-256)..."
      ARCHIVE="${DECRYPT_DIR}/volumes_${BACKUP_PREFIX}.tar.gz"
      # A failed decrypt must abort loudly — never feed an undecrypted stream
      # to tar below.
      if ! openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 -salt \
           -pass env:BACKUP_PASSPHRASE -in "$ACTUAL_FILE" -out "$ARCHIVE"; then
        echo "   ✗ Failed to decrypt ${ACTUAL_FILE##*/} — volumes restore aborted" >&2
        rm -rf "$TEMP_DIR" "$DECRYPT_DIR"
        exit 1
      fi
    fi
    # The combined archive must be a valid outer tar — a corrupt or
    # wrong-format backup must fail loudly here, not pretend to restore.
    if ! tar -xzf "$ARCHIVE" -C "$TEMP_DIR"; then
      echo "   ✗ Combined volume archive is not a valid tar: $ACTUAL_FILE" >&2
      rm -rf "$TEMP_DIR" "$DECRYPT_DIR"
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

    rm -rf "$TEMP_DIR" "$DECRYPT_DIR"
    if [ "$restored" -eq 0 ]; then
      echo "   ✗ No per-volume archives inside $ACTUAL_FILE — nothing restored" >&2
      exit 1
    fi
    echo "   ✓ Volumes restored (${restored} volume(s))"
  else
    echo "   ⚠ Volumes backup not found: ${VOLUMES_FILE}"
  fi
fi

# ── Restore Traefik (ACME/SSL) ───────────────
# backup.sh tars /etc/traefik from the RUNNING traefik container into
# traefik_<ts>.tar.gz; without this step that ACME/TLS material could never
# come back. Mirror the volumes flow: validate + extract to TEMP_DIR, push
# the files into the traefik-data volume via a throwaway alpine container,
# then bring traefik back so the ACME state is live again. Runs whenever we
# are NOT --pg-only (the ACME/TLS material is volume-like, not SQL).
if [ "$PG_ONLY" = false ]; then
  resolve_artifact "$TRAEFIK_BASE"
  if [ -n "$ACTUAL_FILE" ]; then
    echo "   → Restoring Traefik (ACME/SSL)..."
    PROJECT_NAME="${COMPOSE_PROJECT_NAME:-opensme-compose}"
    TEMP_DIR="${BACKUP_DIR}/restore-temp-${BACKUP_PREFIX}"
    DECRYPT_DIR="${BACKUP_DIR}/decrypt-temp-${BACKUP_PREFIX}"

    mkdir -p "$TEMP_DIR" "$DECRYPT_DIR"
    ARCHIVE="$ACTUAL_FILE"
    if [ "$IS_ENCRYPTED" = true ]; then
      echo "   🔓 Decrypting ${ACTUAL_FILE##*/} (openssl AES-256)..."
      ARCHIVE="${DECRYPT_DIR}/traefik_${BACKUP_PREFIX}.tar.gz"
      if ! openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 -salt \
           -pass env:BACKUP_PASSPHRASE -in "$ACTUAL_FILE" -out "$ARCHIVE"; then
        echo "   ✗ Failed to decrypt ${ACTUAL_FILE##*/} — Traefik restore aborted" >&2
        rm -rf "$TEMP_DIR" "$DECRYPT_DIR"
        exit 1
      fi
    fi
    # The archive must be a valid tar — a corrupt or wrong-format backup must
    # fail loudly here, not silently leave the ACME state half-restored.
    if ! tar -xzf "$ARCHIVE" -C "$TEMP_DIR"; then
      echo "   ✗ Traefik archive is not a valid tar: $ACTUAL_FILE" >&2
      rm -rf "$TEMP_DIR" "$DECRYPT_DIR"
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
      rm -rf "$TEMP_DIR" "$DECRYPT_DIR"
      echo "   ✓ Traefik restored"
    else
      rm -rf "$TEMP_DIR" "$DECRYPT_DIR"
      echo "   ✗ Traefik restore failed (ACME state NOT written)" >&2
      exit 1
    fi

    # Bring traefik back so the ACME state is live again
    docker compose up -d traefik 2>/dev/null || true
    echo "   ✓ Traefik restarted"
  else
    echo "   ⚠ Traefik backup not found: ${TRAEFIK_FILE}"
  fi
fi

# ── Restart services ──────────────────────────
echo "🚀 Restarting services..."
docker compose up -d
sleep 15

echo ""
echo "✅ Restore complete!"
echo "   Check service health: docker compose ps"
echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restore completed: ${BACKUP_PREFIX}" >> "${BACKUP_DIR}/restore.log"
