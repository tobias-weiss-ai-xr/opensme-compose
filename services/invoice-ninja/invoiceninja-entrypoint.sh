#!/bin/sh
# ── Invoice Ninja entrypoint ─────────────────────────────────
# openSME integration for Invoice Ninja v5 (Laravel-based).
# Similar pattern to mail/sogo/sogo-entrypoint.sh: template-based
# config injection + bootstrap prep, then delegate to upstream.
#
# This script runs as root (USER 0) before the upstream entrypoint
# drops privileges. All setup is done while root.

set -e

export INVOICENINJA_HOME=/var/www/app

echo "[invoiceninja-entrypoint] Starting openSME Invoice Ninja setup..."

# ── 1. Ensure directories exist ────────────────────────────────
mkdir -p "${INVOICENINJA_HOME}/bootstrap/cache" \
         "${INVOICENINJA_HOME}/storage/app/public" \
         "${INVOICENINJA_HOME}/storage/logs"

# ── 2. Trust anchor for OIDC backchannel ──────────────────────
# Same pattern as SOGo: fold the lab wildcard into the system CA bundle
# while we are still root (needed for https://auth.<domain> verification).
if [ -f /usr/local/share/ca-certificates/opensme-lab.crt ] \
  && command -v update-ca-certificates >/dev/null 2>&1; then
  update-ca-certificates >/dev/null 2>&1 \
    && echo "[invoiceninja-entrypoint] Lab CA folded into system bundle" \
    || echo "[invoiceninja-entrypoint] WARN: update-ca-certificates failed"
fi

# ── 3. Laravel bootstrap: clear stale cache, cache fresh config ──
if [ -f "${INVOICENINJA_HOME}/artisan" ]; then
    echo "[invoiceninja-entrypoint] Caching Laravel bootstrap..."
    
    php "${INVOICENINJA_HOME}/artisan" config:clear >/dev/null 2>&1 || true
    php "${INVOICENINJA_HOME}/artisan" cache:clear >/dev/null 2>&1 || true
    php "${INVOICENINJA_HOME}/artisan" config:cache >/dev/null 2>&1 || true
    php "${INVOICENINJA_HOME}/artisan" storage:link >/dev/null 2>&1 || true
    
    echo "[invoiceninja-entrypoint] Laravel bootstrap cached"
fi

# ── 4. Run migrations (idempotent) ────────────────────────────
if [ -f "${INVOICENINJA_HOME}/artisan" ]; then
    echo "[invoiceninja-entrypoint] Waiting for database..."
    
    # Wait for the invoiceninja-db service to be healthy
    # (it's on the same network via network_mode: service:invoiceninja-web)
    for _ in $(seq 1 60); do
        if nc -z invoiceninja-db 3306 2>/dev/null; then
            echo "[invoiceninja-entrypoint] Database reachable"
            break
        fi
        sleep 2
    done
    
    echo "[invoiceninja-entrypoint] Running migrations..."
    php "${INVOICENINJA_HOME}/artisan" migrate --force --no-interaction 2>&1 \
        | while read line; do echo "[invoiceninja-entrypoint] migration: $line"; done \
        || echo "[invoiceninja-entrypoint] migrations: no new migrations to run"
fi

echo "[invoiceninja-entrypoint] Setup complete, delegating to upstream..."

# ── Delegate to upstream container entrypoint ──────────────────
exec /entrypoint "$@"
