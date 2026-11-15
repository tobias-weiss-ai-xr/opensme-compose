# invoiceninja/invoiceninja:5 is Alpine-based and ships only
# pdo_mysql/pdo_sqlite — postgres deployments need the pgsql driver.
#
# openSME customizations:
# - pdo_pgsql: PostgreSQL support (uses MariaDB in openSME but keeps build flexible)
# - nc: netcat for database reachability checks in entrypoint
# - ca-certificates: TLS trust anchors for OIDC backchannel
#
# NOTE: no composer packages are installed. The OIDC Socialite provider is
# built into Invoice Ninja itself (app/Libraries/OAuth/Providers/Oidc/) —
# the former `composer require socialiteproviders/oidc` was a no-op for SSO
# and now fails on current :5 digests (upstream composer.json ships a path
# repository (../admin-api) that only exists in their build env, and the
# packagist package was removed anyway).
FROM invoiceninja/invoiceninja:5

USER root

# Install build dependencies and PHP extensions. postgresql-libs stays
# installed (runtime libpq.so.5 for pdo_pgsql); only the -dev headers are
# virtualised and removed again.
RUN apk add --no-cache postgresql-libs \
    && apk add --no-cache --virtual .build-deps \
        postgresql-dev \
    && docker-php-ext-install pdo_pgsql \
    && apk del .build-deps

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
