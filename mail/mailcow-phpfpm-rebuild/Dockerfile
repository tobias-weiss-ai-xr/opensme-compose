# mailcow phpfpm CVE-Fix Rebuild
# Basis: Alpine 3.21 -> 3.23.7 (behebt OpenSSL CVE-2026-31789, libcrypto3 3.3.5->3.5.9)
# Original: data/Dockerfiles/phpfpm/Dockerfile (mailcow-dockerized submodule, commit 81f6f7b0)
#
# WARUM KEIN INTERNET IM BUILD:
#   Docker-Container auf vhrz2392 haben KEIN egress (Proxy 137.248.1.2:3128
#   funktioniert NUR Host-seitig via curl --proxy, NICHT in RUN).
#   -> PECL-Tarballs + composer-setup.php werden per fetch-sources.sh lokal
#      heruntergeladen und per COPY ins Image gelegt (kein Netz im Build noetig).
#
# Build:
#   ./fetch-sources.sh
#   sudo docker buildx create --use --name mc-builder --driver docker-container \
#        --driver-opt network=host
#   sudo docker buildx build --no-cache --load -t ghcr.io/mailcow/phpfpm:8.2.29-4 .
#   sudo sed -i "s|phpfpm:8.2.29-3|phpfpm:8.2.29-4|" \
#        /opt/opensme-compose/mail/mailcow-dockerized/docker-compose.yml
#   cd /opt/opensme-compose/mail/mailcow-dockerized && \
#        sudo docker compose up -d --no-deps php-fpm-mailcow
FROM registry.hrz.uni-marburg.de/dockerio-cache/library/php:8.2-fpm-alpine3.23

LABEL maintainer="The Infrastructure Company GmbH <info@servercow.de> (local CVE-fix rebuild)"

COPY pkg/APCu.tgz pkg/imagick-3.8.1.tgz pkg/mailparse-3.2.0.tgz pkg/memcached-3.4.0.tgz pkg/redis-6.3.0.tgz /tmp/pecl/
COPY composer-setup.php /tmp/

# Laufzeit-Pakete (BLEIBEN im Image; ohne --virtual!)
RUN apk add -U --no-cache aspell bash c-client cyrus-sasl-dev freetype icu-libs imagemagick libavif libjpeg-turbo libmemcached libpng libressl librsvg libwebp libxml2 libxpm libzip mysql-client openldap-dev redis samba-client zlib tzdata gmp gettext gnupg jq re2c

# Build-Werkzeuge (--virtual .build-deps, werden am ENDE entfernt)
RUN apk add -U --no-cache --virtual .build-deps autoconf aspell-dev freetype-dev g++ gettext-dev icu-dev imagemagick-dev imap-dev libavif-dev libjpeg-turbo-dev libmemcached-dev libpng-dev libressl-dev libwebp-dev libxml2-dev libxpm-dev libzip-dev linux-headers make openldap-dev pcre-dev zlib-dev aspell-libs libtool gmp-dev \
  && pecl install /tmp/pecl/APCu.tgz \
  && pecl install /tmp/pecl/imagick-3.8.1.tgz \
  && pecl install /tmp/pecl/mailparse-3.2.0.tgz \
  && pecl install /tmp/pecl/memcached-3.4.0.tgz \
  && pecl install /tmp/pecl/redis-6.3.0.tgz \
  && docker-php-ext-enable apcu imagick memcached mailparse redis \
  && rm -rf /tmp/pecl \
  && php /tmp/composer-setup.php --version=2.10.2 \
  && mv /var/www/html/composer.phar /usr/local/bin/composer \
  && chmod +x /usr/local/bin/composer \
  && rm -f /tmp/composer-setup.php

RUN docker-php-ext-configure intl && docker-php-ext-configure exif
RUN docker-php-ext-configure gd --with-freetype=/usr/include/ --with-jpeg=/usr/include/ --with-webp --with-xpm --with-avif
RUN docker-php-ext-install -j4 exif gd gettext intl ldap opcache pcntl pdo pdo_mysql pspell soap sockets zip bcmath gmp
RUN docker-php-ext-configure imap --with-imap --with-imap-ssl && docker-php-ext-install -j4 imap

# NUR die -dev/.build-deps entfernen (Laufzeit-Shared-Libs MÜSSEN bleiben!)
RUN apk del --purge .build-deps autoconf aspell-dev freetype-dev g++ gettext-dev icu-dev imagemagick-dev imap-dev libavif-dev libjpeg-turbo-dev libmemcached-dev libpng-dev libressl-dev libwebp-dev libxml2-dev libxpm-dev libzip-dev linux-headers make openldap-dev pcre-dev zlib-dev libtool

COPY docker-entrypoint.sh /
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["php-fpm"]
