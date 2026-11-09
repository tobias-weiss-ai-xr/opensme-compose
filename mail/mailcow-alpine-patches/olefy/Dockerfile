FROM ghcr.io/mailcow/olefy:1.16
USER root
COPY apk/*.apk /tmp/up/
RUN apk add --allow-untrusted --force-overwrite /tmp/up/*.apk && rm -rf /tmp/up
