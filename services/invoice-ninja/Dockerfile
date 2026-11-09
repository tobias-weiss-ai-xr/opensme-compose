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

# Install the OIDC Socialite provider
RUN composer require socialiteproviders/oidc --no-interaction --no-update && \
    composer update socialiteproviders/oidc --no-interaction

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
