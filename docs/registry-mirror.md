# Registry mirror — registry.openedusuite.org

A self-hosted pull-through mirror for the openedusuite suite images.

## URLs

| Source | Mirror |
|---|---|
| `ghcr.io/tobias-weiss-ai-xr/openedusuite/<name>:<tag>` | `registry.openedusuite.org/tobias-weiss-ai-xr/openedusuite/<name>:<tag>` |

The repo path is identical — only the registry hostname changes (a clean
host-swap for cluster/compose refs).

## How it works

`registry:2` runs in **pull-through proxy mode** (`REGISTRY_PROXY_REMOTEURL:
https://ghcr.io`). On first `docker pull` of a name it fetches the manifest +
blobs from ghcr, caches them in the `opensme-registry` volume, and serves them
locally afterwards. It is **not** a one-way upload replica — it lazily routes
to ghcr on demand.

The container is reachable only on the internal `opensme-net` network (no host
ports published). A Traefik **file-provider** router (`registry-standin.yml` in
the dynamic dir) fronts it with the `registry.openedusuite.org` vhost + a
Let's Encrypt cert. File-provider routing (not docker labels) is used for the
same reason as the portal/webmail stand-ins: the fleet traefik's docker
provider event stream dies after docker daemon restarts on the OpenVZ host.

## Set up

```console
# on the host:
mkdir -p /opt/opensme-registry
cp registry-compose.yml .env.example /opt/opensme-registry/
# edit /opt/opensme-registry/.env — set REGISTRY_PROXY_PASSWORD to a
# read-scoped PAT (required while the ghcr packages remain private)
cd /opt/opensme-registry
docker compose --env-file .env up -d
```

Then place a file-provider router at
`/root/git/docker-traefik/dynamic/registry-standin.yml` pointing at the
registry container's opensme-net IP:5000. (Managed via the `opensme_suite`
ansible role like the other stand-ins; the backend IP changes whenever the
container is recreated, so the router is re-rendered on deploy.)

## Images mirrored to ghcr (source of truth)

The suite images are pushed to `ghcr.io/tobias-weiss-ai-xr/openedusuite/*`
from a workstation mirror script. The current set:

traefik, postgres, pgbouncer, redis, memcached, zitadel, zitadel-login,
opencloud-rolling, collabora, stalwart, operaton, mariadb, nginx,
paperless-ngx, gotenberg, tika, impress-backend, impress-frontend,
impress-y-provider, synapse, element-web, limesurvey, rclone, invoiceninja,
portal, sogo6-server, sogo6-ui.

## Verify

```console
curl -s https://registry.openedusuite.org/v2/          # -> {}
docker pull registry.openedusuite.org/tobias-weiss-ai-xr/openedusuite/redis:alpine
```
