# mailcow Alpine-CVE-Patches (dovecot + olefy)

Zwei mailcow-Images hatten noch kritische Alpine-3.21-CVEs (identisches Muster
zum phpfpm, aber ohne Build-Werkzeuge - reine APK-Runtime-Upgrades):

| Image | Basis | Vorher (Critical) | Nachher | Fix |
|-------|-------|-------------------|---------|-----|
| dovecot:2.3.21.1-2 | alpine 3.21.6 | 16 alpine CVEs (bind CVE-2026-3593, openssl CVE-2026-31789, mariadb CVE-2026-44170/72) | 0 | apk upgrade |
| olefy:1.15 | alpine 3.21.3 | 3 alpine CVEs (openssl CVE-2026-31789) | 0 | lokale libcrypto3/libssl3/openssl 3.3.7 .apk |

## dovecot (einfacher Weg: apk upgrade)
Die dovecot-Basis erreicht dl-cdn.alpinelinux.org (3.21.6 -> 3.21.9).
sudo docker build --no-cache -t ghcr.io/mailcow/dovecot:2.3.21.1-2-patched .

## olefy (kein Repo-Egress -> lokale .apk via HTTP-Proxy)
Die olefy-Basis erreicht dl-cdn NICHT (exit 99 bei apk update; Basis laeuft als
nicht-root -> USER root im Build noetig). Lade die OpenSSL-Fix-Pakete Host-seitig
ueber den HTTP-only-Proxy (NOT https!) von dl-cdn.alpinelinux.org:
mkdir -p apk
curl -sS --proxy http://137.248.1.2:3128 -o apk/libcrypto3.apk http://dl-cdn.alpinelinux.org/alpine/v3.21/main/x86_64/libcrypto3-3.3.7-r2.apk
curl -sS --proxy http://137.248.1.2:3128 -o apk/libssl3.apk http://dl-cdn.alpinelinux.org/alpine/v3.21/main/x86_64/libssl3-3.3.7-r2.apk
curl -sS --proxy http://137.248.1.2:3128 -o apk/openssl.apk http://dl-cdn.alpinelinux.org/alpine/v3.21/main/x86_64/openssl-3.3.7-r2.apk
sudo docker build --no-cache -t ghcr.io/mailcow/olefy:1.15-patched .

## Deploy (beide)
cd /opt/opensme-compose/mail/mailcow-dockerized
sudo sed -i "s|image: ghcr.io/mailcow/dovecot:2.3.21.1-2$|image: ghcr.io/mailcow/dovecot:2.3.21.1-2-patched|" docker-compose.yml
sudo sed -i "s|image: ghcr.io/mailcow/olefy:1.15$|image: ghcr.io/mailcow/olefy:1.15-patched|" docker-compose.yml
sudo docker compose up -d --no-deps dovecot-mailcow
sudo docker compose up -d --no-deps olefy-mailcow

## Verifikation
sudo docker run --rm --entrypoint sh ghcr.io/mailcow/dovecot:2.3.21.1-2-patched -c "apk list --installed | grep -E \"libcrypto3|bind-libs|mariadb-client \""
# -> 3.3.7-r2, 9.18.50-r0, 11.4.13-r0
sudo docker run --rm --entrypoint sh ghcr.io/mailcow/olefy:1.15-patched -c "openssl version"  # OpenSSL 3.3.7
# Trivy-Critical-Scan je Bild -> alpine 0

## Caveats
- Patched Tags sind lokal, existieren NICHT auf GitHub. Bei Mailcow-up und
  Rollback die Tag-Zeilen zuruecksetzen (Submodule ist ignore=dirty -> diese
  Aenderungen gehen bei Submodule-Update verloren).
- dovecot hat im gosu-binary noch 1 nicht-apk-fixbare CVE (go-binary) - Restrisiko.
