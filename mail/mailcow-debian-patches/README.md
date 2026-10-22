# mailcow Debian-CVE-Patches (postfix + sogo)

Zwei mailcow-Images waren DEBIAN-basiert (kein apk) mit kritischen Perl/libglib-CVEs.
Loesung: `apt-get update && apt-get upgrade -y` in einem Patch-Layer.

| Image | Distro | Vorher (Critical) | Nachher | Haeufige CVEs |
|-------|--------|-------------------|---------|---------------|
| postfix:3.10.12-1 | debian 13.7 | 13 | 0 | perl CVE-2026-13221/8376/42496, libglib2.0 CVE-2026-58016 |
| sogo:5.12.11-1 | debian 12.15 | 12+1 | 0 (+gosu gobinary) | perl CVE-2026-13221/8376/42496, stdlib CVE-2025-68121 |

## Build (beide)
```sh
cd mail/mailcow-debian-patches
sudo docker build --no-cache -t ghcr.io/mailcow/postfix:3.10.12-1-patched postfix/
sudo docker build --no-cache -t ghcr.io/mailcow/sogo:5.12.11-1-patched sogo/
```
(Die Debian-Basis erreicht das apt-Repo -> einfaches apt upgrade reicht.)

## Deploy
```sh
cd /opt/opensme-compose/mail/mailcow-dockerized
sudo sed -i "s|image: ghcr.io/mailcow/postfix:3.10.12-1$|image: ghcr.io/mailcow/postfix:3.10.12-1-patched|" docker-compose.yml
sudo sed -i "s|image: ghcr.io/mailcow/sogo:5.12.11-1$|image: ghcr.io/mailcow/sogo:5.12.11-1-patched|" docker-compose.yml
sudo docker compose up -d --no-deps postfix-mailcow
sudo docker compose up -d --no-deps sogo-mailcow
```

## Verifikation
```sh
sudo docker exec mailcowdockerized-postfix-mailcow-1 sh -c "postfix status"  # running
sudo docker exec mailcowdockerized-sogo-mailcow-1 sh -c "ls /etc/sogo/sogo.conf"
# Trivy-Critical je Bild -> debian 0
```

## Caveats
- Patched Tags sind lokal (nicht auf GitHub). Bei Mailcow-up Tag-Aenderungen
  zuruecksetzen (Submodule ignore=dirty -> gehen bei Submodule-Update verloren).
- sogo hat 1 verbleibenden gosu-GObinary-CVE (nicht OS-fixbar, minimal).
- postfix perl blieb 5.40.1-6+deb13u1 (fix deb13u2 evtl. nicht im aktuellen
  Repo-Index); Trivy ignore-unfixed meldet es deswegen nicht (NOT-FIXED).
