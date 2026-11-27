# Standalone Events Gateway (opt-in)

The gateway is an **optional**, read-only SSE endpoint for the portal event
bus. It exists so SSE readers can be isolated from portal writes if suite-wide
load ever demands it. It is **disabled by default**; the normal
`GET /api/events` route on the portal remains the production path.

The gateway shares `portal/src/events.rs` via a Rust path module. Framing,
`Last-Event-ID` resume, cursor sanitisation, visibility filtering, heartbeats
and per-viewer stream limits are therefore byte-for-byte the portal contract.
It does **not** publish events: publishing stays in the portal process.

## Enable

Required environment (same values as the portal):

| Variable | Required | Meaning |
|---|---|---|
| `EVENTS_ENABLED` | yes (`1` or `true`) | Enables stream reads; otherwise 204 |
| `REDIS_URL` | yes | `redis://:password@redis:6379` |
| `EVENTS_STREAM` | no | Defaults to `opensme:events` |
| `EVENTS_MAXLEN` | no | Defaults to `10000` |
| `OPENSME_DOMAIN` | yes | Tenant used to build the viewer |
| `LOGIN_SESSION_COOKIE_SECRET` | yes | HMAC key for `OPENSMEPORTALSESSION`; weak/CHANGEME values are fail-closed |
| `GATEWAY_PORT` | no | Listen port, default `8081` |

```bash
COMPOSE_PROFILES=events-gateway docker compose up -d events-gateway
```

Traefik then routes `Host(events.<OPENSME_DOMAIN>) && PathPrefix(/api/events)`
to the gateway. No other path is exposed.

## Verify

With a currently valid `OPENSMEPORTALSESSION` cookie from the portal:

```bash
curl -N -i \
  -H 'Cookie: OPENSMEPORTALSESSION=<signed-cookie>' \
  'https://events.<domain>/api/events?since=1234567890000-0'
```

Expected: `200 text/event-stream`, an `event: ready` frame after the first
cursor check, then live `event: <kind>` frames with `id:` fields. Without a
cookie, with a tampered/expired cookie, or with a weak
`LOGIN_SESSION_COOKIE_SECRET`, the response is `401` with
`{"error":"session invalid or expired"}`. With `EVENTS_ENABLED` unset, the
gateway returns `204`, matching the portal contract.

## Rollback

```bash
COMPOSE_PROFILES=events-gateway docker compose stop events-gateway
```

The portal's own `/api/events` route is unaffected and remains the default.
No Redis data is changed by enabling or disabling the gateway.

## Contract guard

`tests/00-static/check_events_gateway.py` pins the crate, the shared events
module, cookie verification, the opt-in compose profile, the read-only route
and this runbook. The guard is registered in `tests/run.py` and has mutation
teeth in `tests/00-static/check_mutations.py` (`gateway-session-verification-removed`).
