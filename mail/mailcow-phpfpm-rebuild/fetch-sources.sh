#!/bin/sh
# Laedt PECL-Tarballs + composer-setup.php lokal herunter (Proxy nötig, nur Host-seitig)
# Nutzung: sudo ./fetch-sources.sh
set -e
PROXY=http://137.248.1.2:3128
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DIR/pkg"
cd "$DIR"

echo ">> APCu (latest)"
curl -sS --proxy "$PROXY" --connect-timeout 8 https://pecl.php.net/get/APCu -o pkg/APCu.tgz
echo ">> imagick-3.8.1"
curl -sS --proxy "$PROXY" --connect-timeout 8 https://pecl.php.net/get/imagick-3.8.1.tgz -o pkg/imagick-3.8.1.tgz
echo ">> mailparse-3.2.0"
curl -sS --proxy "$PROXY" --connect-timeout 8 https://pecl.php.net/get/mailparse-3.2.0.tgz -o pkg/mailparse-3.2.0.tgz
echo ">> memcached-3.4.0"
curl -sS --proxy "$PROXY" --connect-timeout 8 https://pecl.php.net/get/memcached-3.4.0.tgz -o pkg/memcached-3.4.0.tgz
echo ">> redis-6.3.0"
curl -sS --proxy "$PROXY" --connect-timeout 8 https://pecl.php.net/get/redis-6.3.0.tgz -o pkg/redis-6.3.0.tgz
echo ">> composer-setup.php"
curl -sS --proxy "$PROXY" --connect-timeout 8 https://getcomposer.org/installer -o composer-setup.php

echo
echo "Download fertig. Pruefe Groessen (APCu+4 Pakete sollten >0 sein):"
ls -la pkg/ composer-setup.php
