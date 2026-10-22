# mailcow phpfpm CVE-Fix Rebuild

## Problem
`ghcr.io/mailcow/phpfpm:8.2.29-3` (Basis `php:8.2-fpm-alpine3.21`) hat
**3 CRITICAL + 51 HIGH** Trivy-CVEs, darunter **OpenSSL CVE-2026-31789**
(libcrypto3 3.3.5-r0, heap-buffer-overflow). Mailcow upstream veroeffentlicht
KEINE neueren Images. HRZ-Mirror hat nur docker.io (nicht ghcr.io) aber
`dockerio-cache/library/php:8.2-fpm-alpine3.23` (mit libcrypto3 3.5.9).

## Loesung
Lokaler Rebuild mit **Alpine 3.23** Basis. Ergebnis:
`ghcr.io/mailcow/phpfpm:8.2.29-4` — **0 CVEs** (Trivy, ignore-unfixed).

## Warum lokale PECL-Tarballs?
Docker-Container auf vhrz2392 haben **kein Internet** (Proxy 137.248.1.2:3128
funktioniert nur Host-seitig via curl, nicht in RUN). Daher werden die
PECL/Tarballs + Composer per `fetch-sources.sh` lokal geladen und per COPY
ins Image gelegt.

## Build + Deploy (auf vhrz2392, als root)
```sh
cd /opt/opensme-compose/mail/mailcow-phpfpm-rebuild
sudo ./fetch-sources.sh          # laedt pkg/*.tgz + composer-setup.php

sudo docker buildx create --use --name mc-builder \
     --driver docker-container --driver-opt network=host
sudo docker buildx build --no-cache --load \
     -t ghcr.io/mailcow/phpfpm:8.2.29-4 .

# Tag im Submodule auf neuen Build zeigen lassen:
sudo sed -i "s|phpfpm:8.2.29-3|phpfpm:8.2.29-4|" \
     /opt/opensme-compose/mail/mailcow-dockerized/docker-compose.yml
cd /opt/opensme-compose/mail/mailcow-dockerized
sudo docker compose up -d --no-deps php-fpm-mailcow
```

## Verifikation
```sh
sudo docker run --rm --entrypoint php ghcr.io/mailcow/phpfpm:8.2.29-4 -m  # keine Warnungen
sudo docker run --rm --entrypoint sh ghcr.io/mailcow/phpfpm:8.2.29-4 -c "apk list --installed | grep libcrypto3" # 3.5.9-r0
sudo docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v /root/.cache/trivy:/root/.cache/trivy \
  aquasec/trivy:latest image --cache-backend memory --ignore-unfixed --scanners vuln \
  --no-progress ghcr.io/mailcow/phpfpm:8.2.29-4   # 0 CVEs
```

## Caveats
- **Lokales Image**: Tag `8.2.29-4` existiert NICHT auf GitHub. Bei einem
  kuenftigen `mailcow.sh up` (mit `--pull`) wuerde das Tag versucht zu pullen
  und scheitern -> vor Upgrade auf neues Mailcow die `docker-compose.yml`-Zeile
  zurueck auf ein existierendes ghcr-Tag setzen (oder neu bauen).
- **Mailcow-Submodule bleibt unberuehrt** (ignore=dirty). Der Rebuild lebt
  NUR in diesem Verzeichnis + als lokales Docker-Image.
- `data/conf/postfix/main.cf` (DNSBL-Reihenfolge + myhostname=mail.opensme.org)
  ist eine Fremde, parallele Aenderung — nicht Teil dieses Rebuilds.

## Warum Form-und-fehler-Fallen (fuer naechstes Mal)
- `pecl clear-cache` -> Non-Zero-Exit wenn /tmp/pear/cache fehlt -> entfernen.
- Composer installiert nach `/var/www/html/composer.phar` (WORKDIR), nicht cwd.
- `apk del .build-deps` entfernt auch Laufzeit-Shared-Libs; NUR -dev-Pakete loeschen.
- `gmp` braucht `gmp-dev`; `aspell` (Laufzeit) fuer pspell noetig.
- APCu-5.1.28 existiert nicht mehr; `pecl.php.net/get/APCu` (ohne Version) = latest.
