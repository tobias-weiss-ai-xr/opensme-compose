# openSME — Resource Baselines

_Generated 2026-08-23 17:56 UTC_ by `tests/00-static/check_perf.py --write-baselines`.

Merged `docker compose` models (pure-YAML, no engine needed). Budgets are **reservation** sums (what the scheduler guarantees); limits cap spikes. Profile-gated overlays are included per tier.

| Tier | Host | Σlimits | Σreservations | Budget (res) | Status |
|------|------|---------|---------------|--------------|--------|
| soho | 4c/8G | 2.07G | 0.91G | ≤ 6G | ✓ |
| small | 8c/24G | 8.16G | 3.46G | ≤ 20G | ✓ |
| medium | 16c/48G | 25.79G | 8.23G | ≤ 40G | ✓ |

## Per-tier services

### soho

| Service | Image | Limits | Reservations |
|---------|-------|--------|--------------|
| memcached | `${MEMCACHED_IMAGE:-memcached:1.6-alpine}` | 0.25c/128M | 0.1c/32M |
| portal | `(build)` | 0.5c/128M | 0.1c/32M |
| postgres | `${POSTGRES_IMAGE:-postgres:17-alpine}` | 1c/512M | 0.5c/256M |
| redis | `${REDIS_IMAGE:-redis:7-alpine}` | 0.25c/192M | 0.1c/96M |
| traefik | `${TRAEFIK_IMAGE:-traefik:v3.3}` | 1c/256M | 0.25c/128M |
| zitadel | `ghcr.io/zitadel/zitadel:latest` | 1c/512M | 0.5c/256M |
| zitadel-login | `ghcr.io/zitadel/zitadel-login:v4.19.3` | 0.5c/384M | 0.1c/128M |

### small

| Service | Image | Limits | Reservations |
|---------|-------|--------|--------------|
| invoiceninja | `invoiceninja/invoiceninja:5` | 0.25c/192M | 0.1c/32M |
| memcached | `${MEMCACHED_IMAGE:-memcached:1.6-alpine}` | 1c/512M | 0.25c/128M |
| opencloud | `opencloudeu/opencloud-rolling:6.0.0` | 2c/2G | 1c/1G |
| paperless-gotenberg | `gotenberg/gotenberg:8` | 0.10c/96M | 0.05c/16M |
| paperless-ngx | `ghcr.io/paperless-ngx/paperless-ngx:latest` | 0.50c/384M | 0.25c/64M |
| paperless-tika | `apache/tika:latest` | 0.50c/512M | 0.25c/128M |
| pgbouncer | `bitnamilegacy/pgbouncer:1.23.0` | 0.5c/256M | 0.1c/64M |
| portal | `(build)` | 0.5c/128M | 0.1c/32M |
| postgres | `${POSTGRES_IMAGE:-postgres:17-alpine}` | 2c/1G | 1c/512M |
| redis | `${REDIS_IMAGE:-redis:7-alpine}` | 1c/1.5G | 0.5c/768M |
| traefik | `${TRAEFIK_IMAGE:-traefik:v3.3}` | 1c/256M | 0.25c/128M |
| zitadel | `ghcr.io/zitadel/zitadel:latest` | 1c/1G | 0.5c/512M |
| zitadel-login | `ghcr.io/zitadel/zitadel-login:v4.19.3` | 0.5c/384M | 0.1c/128M |

### medium

| Service | Image | Limits | Reservations |
|---------|-------|--------|--------------|
| collabora | `collabora/code:24.04.13.3.1` | 2c/3G | 1c/1G |
| invoiceninja | `invoiceninja/invoiceninja:5` | 0.25c/192M | 0.1c/32M |
| memcached | `${MEMCACHED_IMAGE:-memcached:1.6-alpine}` | 1c/1.5G | 0.25c/192M |
| minio | `minio/minio:latest` | 2c/2G | 0.5c/256M |
| notes-backend | `lasuite/impress:1.14.0-backend` | 0.35c/256M | 0.1c/32M |
| notes-frontend | `lasuite/impress:1.14.0-frontend` | 0.10c/64M | 0.05c/8M |
| notes-y-provider | `lasuite/impress-y-provider:v4.4.0` | 0.15c/64M | 0.05c/16M |
| opencloud | `opencloudeu/opencloud-rolling:6.0.0` | 3c/4G | 1c/1.5G |
| paperless-gotenberg | `gotenberg/gotenberg:8` | 0.10c/96M | 0.05c/16M |
| paperless-ngx | `ghcr.io/paperless-ngx/paperless-ngx:latest` | 0.50c/384M | 0.25c/64M |
| paperless-tika | `apache/tika:latest` | 0.50c/512M | 0.25c/128M |
| pgbouncer | `bitnamilegacy/pgbouncer:1.23.0` | 1c/512M | 0.25c/128M |
| portal | `(build)` | 1c/256M | 0.25c/64M |
| postgres | `${POSTGRES_IMAGE:-postgres:17-alpine}` | 4c/4G | 1c/2G |
| redis | `${REDIS_IMAGE:-redis:7-alpine}` | 2c/3G | 0.5c/1G |
| sogo | `${SOGO_IMAGE:-salvoxia/sogo:latest}` | 2c/2G | 0.5c/512M |
| stalwart | `${STALWART_IMAGE:-stalwartlabs/stalwart:v0.15.0}` | 2c/2G | 0.5c/512M |
| synapse | `element-hq/synapse:v1.144.0` | 0.50c/384M | 0.25c/64M |
| traefik | `${TRAEFIK_IMAGE:-traefik:v3.3}` | 1c/256M | 0.25c/128M |
| zitadel | `ghcr.io/zitadel/zitadel:latest` | 2c/1G | 1c/512M |
| zitadel-login | `ghcr.io/zitadel/zitadel-login:v4.19.3` | 0.5c/384M | 0.1c/128M |
