# Portal Migration: Rust portal on the public edge

Status: applied (2026-11-05)
Owner: platform / compose
Scope: compose `portal` service env + traefik file provider (`portal-standin.yml`)

## Why

- The public openSME eval stack (`/opt/opensme-compose/compose` on the 1blu
  VPS) serves `https://home.opensme.graphwiz.ai/`.
- The edge traefik runs on the **host network** and uses a **file provider**
  in `$TRAEFIK_DIR/dynamic/` because docker-provider events die
  after a daemon restart on that OpenVZ host.
- The router `portal-standin.yml` (priority 1000) previously forwarded
  `Host(Home)` to the OLD standalone Next.js portal (`opensme-portal-web`,
  host port 3001). That standalone portal's `/api/services` requires the raw
  Zitadel `sessions` browser cookie and returns 401 for programmatic / e2e
  requests.
- The NEW Rust portal (`opensme-portal`, compose service `portal`, built from
  `./portal`, listens on 8080) is attached to `opensme-net` and implements the
  OIDC round trip with its own `OPENSMEPORTALSESSION`
  cookie — its `/api/services` works without a browser cookie.
- The compose `portal` service did not map the OIDC / session secrets, so the
  Rust portal **failed closed** (see `portal/src/session.rs`):
  - startup logs showed `environment variable not set, using default
    key=PORTAL_OIDC_CLIENT_ID / ...SECRET`
  - `/api/auth/login` returned 503.

## What

1. **Compose env** — map three variables into the `portal:` service
   (`docker-compose.yml`), fail-closed (`:?` so compose refuses to start when
   they are unset):
   ```yaml
   PORTAL_OIDC_CLIENT_ID: ${PORTAL_OIDC_CLIENT_ID:?}
   PORTAL_OIDC_CLIENT_SECRET: ${PORTAL_OIDC_CLIENT_SECRET:?}
   LOGIN_SESSION_COOKIE_SECRET: ${LOGIN_SESSION_COOKIE_SECRET:?}
   ```
   Values come from the stack's `.env` (never printed / committed — compose
   only ever references the variables). On the live VPS the `opensme-portal`
   container is attached to `opensme-net` (a private bridge network); the
   host traefik reaches it directly at that bridge IP on port 8080.

2. **Edge retarget** — in
   `$TRAEFIK_DIR/dynamic/portal-standin.yml` change the service
   URL from `http://127.0.0.1:3001` to the Rust portal's reachable address
   `http://<IP>:8080` (keep router + rule + priority + host unchanged). The
   traefik file provider picks the change up within seconds (no reload). Note:
   this host runs the system traefik on the host network with a file provider;
   the compose-built-in traefik (`profiles: ["standalone"]`) and the
   `traefik-web` external network are not used here, so the address is the
   portal's `opensme-net` bridge IP, not a `traefik-web` IP.

## Verify

From `/opt/opensme-compose/compose` on the VPS (as root):

```sh
docker compose -f docker-compose.yml -f idm/zitadel.yml config --quiet
docker inspect opensme-portal --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{$v.IPAddress}}{{"\n"}}{{end}}'
curl -s -m 10 -D - -o /dev/null http://<IP>:8080/api/auth/login -H 'Host: home.opensme.graphwiz.ai'
```

- Internal gate: `/api/auth/login` must return a 3xx whose `Location` starts
  with `https://auth.home.opensme.graphwiz.ai/` (Zitadel v4.19.4 serves
  `/oidc/v1/authorize?client_id=...`) — before the fix it was 503.
- Public: `https://home.opensme.graphwiz.ai/` returns 200 with the Rust portal
  UI.
- Full e2e (VPS, from `/opt/opensme-compose/compose`): run
  `tests/05-e2e/run.py` with the demo credentials (login name `testuser` and
  the documented demo password — see README) exported as
  `E2E_ZITADEL_LOGIN_NAME` and `ZITADEL_ADMIN_PASSWORD`.

  Expect ≥ 31 passed with the portal section fully green, including
  `portal /api/services: N service(s) (signed in)` and no
  `portal /api/services: HTTP 401`. The e2e drives the real
  `/api/auth/login → IdP → /api/auth/callback` round trip and validates
  userinfo authenticated as `testuser`.

## Rollback

Should the edge need to go back to the standalone portal, restore the two
timestamped backups taken before the migration and recreate the portal
(replace `TS` with the timestamp of the backup files created before the
migration):

```sh
TS=<backup-timestamp>
cp /opt/opensme-compose/compose/docker-compose.yml.bak-$TS /opt/opensme-compose/compose/docker-compose.yml
cp "$TRAEFIK_DIR/dynamic/portal-standin.yml.bak-$TS" "$TRAEFIK_DIR/dynamic/portal-standin.yml"
cd /opt/opensme-compose/compose && docker compose -f docker-compose.yml -f idm/zitadel.yml up -d portal
```

After restore, confirm `https://home.opensme.graphwiz.ai/` is served again by
the standalone portal and `opensme-portal-web` is Up.
