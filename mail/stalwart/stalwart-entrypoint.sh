#!/bin/sh
# ── Stalwart entrypoint ────────────────────────
# Renders config.toml.template with env vars into the v0.15 schema, then
# starts Stalwart. The v0.15 image only generates a config when none exists,
# so our rendered file takes full precedence (no interactive wizard).
set -e

TEMPLATE=/etc/stalwart/config.toml.template
OUTDIR=/opt/stalwart/etc
OUTPUT=$OUTDIR/config.toml

# Listener bind address. "[::]" is the right default on a dual-stack host
# (one socket for IPv4 and IPv6), but when the container has no IPv6 stack —
# host booted with ipv6.disable=1, or a daemon without IPv6 in the kernel —
# every [::] bind fails SILENTLY: stalwart stays up, writes no log line and
# serves no port at all, so the container is merely "unhealthy" with no
# explanation. Detect it and fall back to IPv4; STALWART_BIND overrides.
if [ -n "$STALWART_BIND" ]; then
  bind_addr=$STALWART_BIND
  bind_why="from STALWART_BIND"
elif [ -e /proc/net/if_inet6 ]; then
  bind_addr="[::]"
  bind_why="dual-stack"
else
  bind_addr="0.0.0.0"
  bind_why="no IPv6 in this container"
fi
echo "[stalwart-entrypoint] Listening on $bind_addr:25/143/587/8080 ($bind_why)"

mkdir -p "$OUTDIR"
# The tracer writes to /var/lib/stalwart/logs, but the image never creates that
# directory. A missing path means "no log sink" — not "no errors" — so keep the
# server silent-safe instead of undiagnosable.
mkdir -p /var/lib/stalwart/logs

if [ -f "$TEMPLATE" ]; then
  echo "[stalwart-entrypoint] Rendering $TEMPLATE -> $OUTPUT"
  sed \
    -e "s|\${MAIL_DOMAIN}|${MAIL_DOMAIN:-mail.opensme.graphwiz.ai}|g" \
    -e "s|\${STALWART_ADMIN_PASSWORD}|${STALWART_ADMIN_PASSWORD:-CHANGEME_stalwart_admin}|g" \
    -e "s|\${STALWART_BIND}|${bind_addr}|g" \
    -e "s|\${STALWART_PUBLIC_URL}|${STALWART_PUBLIC_URL:-https://mail.opensme.graphwiz.ai}|g" \
    -e "s|\${ZITADEL_DOMAIN}|${ZITADEL_DOMAIN:-auth.opensme.graphwiz.ai}|g" \
    "$TEMPLATE" > "$OUTPUT"
  echo "[stalwart-entrypoint] Done."
else
  echo "[stalwart-entrypoint] No template found, using existing $OUTPUT (if any)"
fi

exec "$@"
