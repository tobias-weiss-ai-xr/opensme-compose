# invoiceninja/invoiceninja:5 is Alpine-based and ships only
# pdo_mysql/pdo_sqlite — postgres deployments need the pgsql driver.
#
# openSME customizations:
# - pdo_pgsql: PostgreSQL support (uses MariaDB in openSME but keeps build flexible)
# - nc: netcat for database reachability checks in entrypoint
# - socialiteproviders/oidc: OIDC Socialite provider for Zitadel SSO
# - ca-certificates: TLS trust anchors for OIDC backchannel
FROM invoiceninja/invoiceninja:5

USER root

# Install build dependencies and PHP extensions
RUN apk add --no-cache --virtual .build-deps \
        postgresql-dev \
        composer \
    && docker-php-ext-install pdo_pgsql \
    && apk del .build-deps

# Install the OIDC Socialite provider. Upstream :5 (>= 2026-11 digest)
# ships a path repository (../admin-api) in composer.json that only exists
# in their build env — any composer command aborts with "The url supplied
# for the path (../admin-api) repository does not exist". Strip path repos
# first; the remaining public VCS repos must clone over https (no SSH
# credentials exist in the build).
RUN git config --global url."https://github.com/".insteadOf "git@github.com:" && \
    php -r '$c=json_decode(file_get_contents("composer.json"),true);$c["repositories"]=array_values(array_filter($c["repositories"]??[],function($r){return ($r["type"]??"")!=="path";}));file_put_contents("composer.json",json_encode($c,JSON_PRETTY_PRINT|JSON_UNESCAPED_SLASHES|JSON_UNESCAPED_UNICODE));' && \
    COMPOSER_ALLOW_SUPERUSER=1 composer require socialiteproviders/oidc --no-interaction --no-update && \
    COMPOSER_ALLOW_SUPERUSER=1 composer update socialiteproviders/oidc --no-interaction --prefer-dist

# Install runtime tools for the entrypoint
RUN apk add --no-cache \
        netcat-openbsd \
        ca-certificates \
        tzdata

# Copy the openSME entrypoint
COPY invoiceninja-entrypoint.sh /invoiceninja-entrypoint.sh
RUN chmod +x /invoiceninja-entrypoint.sh

# Switch back to the container user (invoiceninja runs as UID 1500)
USER 1500
