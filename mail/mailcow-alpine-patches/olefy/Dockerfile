FROM ghcr.io/mailcow/olefy:1.15
USER root
COPY apk/*.apk /tmp/up/
RUN apk add --allow-untrusted --force-overwrite /tmp/up/*.apk && rm -rf /tmp/up
