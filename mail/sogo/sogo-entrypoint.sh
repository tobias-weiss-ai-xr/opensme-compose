#!/bin/sh
# ── SOGo entrypoint ────────────────────────────
# Renders sogo.conf.template with env vars into the persistent config location
# the image expects (/srv/etc/sogo.conf on the sogo-data volume). The image's
# /sogod.sh then copies that to /etc/sogo/sogo.conf and drops privileges via
# `su sogo` (SOGo refuses to run as root). We delegate startup to the image's
# /start.sh so cron/memcached/apache2/sogod all start as designed.
# Uses sed instead of envsubst for maximum image compatibility.
set -e

TEMPLATE=/sogo.conf.template
OUTPUT=/srv/etc/sogo.conf

if [ -f "$TEMPLATE" ]; then
  echo "[sogo-entrypoint] Rendering $TEMPLATE → $OUTPUT"
  mkdir -p "$(dirname "$OUTPUT")"
  # The password is embedded in postgresql:// URLs, so it MUST be
  # percent-encoded: `openssl rand -base64 24` (scripts/init-env.sh) produces
  # '/' and '+', and a raw '/' makes the URL unparsable — SOGo then reports
  #   "default 'OCSFolderInfoURL' is not a valid URL: ..."
  # at startup and every SQL call answers
  #   "failed to acquire channel for URL: (null)"
  # while the login page keeps rendering happily. Encode here (not only at
  # generation time) so a hand-rotated password in .env works too.
  db_pw_url=$(printf '%s' "${SOGO_DB_PASSWORD:-CHANGEME_sogo}" \
    | sed -e 's|%|%25|g' -e 's|/|%2F|g' -e 's|+|%2B|g' -e 's|=|%3D|g' \
    | sed -e 's|[&\\]|\\&|g')
  sed \
    -e "s|\${SOGO_DB_PASSWORD}|${db_pw_url}|g" \
    -e "s|\${LDAP_ROOT_DN}|${LDAP_ROOT_DN:-dc=opensme,dc=graphwiz,dc=ai}|g" \
    -e "s|\${LDAP_ADMIN_PASSWORD}|${LDAP_ADMIN_PASSWORD:-CHANGEME_ldap}|g" \
    -e "s|\${OPENSME_DOMAIN}|${OPENSME_DOMAIN:-opensme.graphwiz.ai}|g" \
    -e "s|\${TZ}|${TZ:-Europe/Berlin}|g" \
    "$TEMPLATE" > "$OUTPUT"
  chown sogo:sogo "$OUTPUT"
  echo "[sogo-entrypoint] Done."
else
  echo "[sogo-entrypoint] No template found, using existing $OUTPUT"
fi

# Delegate to the image's start script (drops to sogo user via su for sogod).
exec sh -c "/start.sh && tail -f /var/log/sogo/sogo.log"
