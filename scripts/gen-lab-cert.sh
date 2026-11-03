#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# openSME — Lab-Zertifikat rotieren (*.opensme.local Wildcard)
# ─────────────────────────────────────────────────────────────────
# Regeneriert das selbstsignierte Wildcard-Zertifikat, das der Edge
# (Traefik) für alle vhosts ohne ACME-Resolver serviert und das die
# OIDC-Backchannel (Paperless allauth, SOGo) als Trust-Anchor nutzen.
#
# Das Zertifikat ist ein COMMITTED Fixture für die nicht-routbare
# Lab-Zone *.opensme.local — kein Geheimnis im Sinne von Produktions-
# PKI. Öffentliche Deployments (ACME/Let's Encrypt pro Router) sind
# davon unberührt.
#
# NACH der Rotation zwingend:
#   1. git add traefik/certs/selfsigned.{crt,key}  (neues Fixture committen)
#   2. make paperless-oidc-image                   (CA ist INS Bild gebacken)
#   3. Stack neu starten (traefik, paperless-ngx, sogo)
#
# Usage: scripts/gen-lab-cert.sh [--force]
#   Ohne --force: überschreibt nichts (idempotent).
set -euo pipefail

CERT_DIR="$(cd "$(dirname "$0")/../traefik/certs" && pwd)"
CERT="$CERT_DIR/selfsigned.crt"
KEY="$CERT_DIR/selfsigned.key"

if [[ -f "$CERT" && "${1:-}" != "--force" ]]; then
  echo "[gen-lab-cert] $CERT existiert bereits — nichts getan (--force zum Rotieren)."
  exit 0
fi

mkdir -p "$CERT_DIR"
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout "$KEY" -out "$CERT" \
  -subj "/CN=opensme-lab-wildcard" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
  -addext "subjectAltName=DNS:*.opensme.local,DNS:opensme.local"

openssl x509 -in "$CERT" -noout -subject -dates -ext subjectAltName
echo "[gen-lab-cert] Erneuert. Jetzt: git add, make paperless-oidc-image, Stack neu starten."
