# VC — self-hosted video meetings (LiveKit)

Minimal LiveKit SFU + a tiny token-mint/room UI. Enabled with `--profile vc`;
both services join the **existing** `opensme-net` — **no new docker networks**.

| Service | Container | Image | Purpose |
|---|---|---|---|
| `vc-livekit` | `opensme-vc-livekit` | `livekit/livekit-server:v1.13.11` | SFU: signaling (7880), media mux (UDP 7882), ICE/TCP fallback (7881) |
| `vc-room` | `opensme-vc-room` | `vc-room:1.0.0` (local build, `images/vc-room/`) | lobby + room pages, mints LiveKit access tokens (offline HMAC) |

## Edge (public surface)

`$VC_DOMAIN` (live: `https://vc.home.opensme.graphwiz.ai`) via the fleet
traefik **file routers** (`vc-livekit-sec` priority 10010 for
`PathPrefix(/rtc)||/twirp` with a static `vc-livekit@file` service →
`http://127.0.0.1:7880`; `vc-room-sec` priority 10005 for the rest →
`vc-room@docker`) — the same shadow pattern as `calendar-sec`. Both
routers chain `[vc-permissions, security-headers]`: the `vc-permissions`
middleware lifts the blanket `camera=()/microphone=()` policy
(camera/mic/screen need `=(self)` on a call page — first middleware in
the chain wins the response write). Priorities sit ABOVE the 10000
label routers so the docker-label routers can never shadow them.
Docker labels in docker-compose.yml are the repo-canonical edge for
repo-local users and are inert on the fleet.

## Networking (host network — deliberate)

`vc-livekit` runs with `network_mode: host`, LiveKit's recommended
docker mode. Reason (this VPS is OpenVZ): UDP into bridge/veth
containers is dead (same kernel quirk family as host→container TCP
flakiness) and the nat table has no DNAT support, so bridge mode +
published ports cannot carry WebRTC media. On the host netns the SFU
binds 7880/7881/7882 directly; the host firewall must allow the media
ports (`ufw allow 7881/tcp`, `ufw allow 7882/udp` — mirrored in
ansible `firewall_rules`). `vc-room` stays on `opensme-net` (HTTP only).

## Ports (host firewall, deliberately minimal)

Healthcheck note: the LiveKit image ships busybox/wget and answers a plain
`GET http://127.0.0.1:7880/` with 200 "OK" — used by the compose healthcheck.

| Port | Proto | Purpose |
|---|---|---|
| 7882/udp | ICE/UDP **mux** — ALL WebRTC media on ONE port (`rtc.udp_port`; `port_range_start/end` intentionally unset). Kept to a single port for the `numiptent` budget. |
| 7881/tcp | ICE/TCP fallback — must be exposed **directly on the node** (no proxy/TLS in front, per LiveKit docs). |

Signaling (7880) is NOT firewall-opened — it rides `wss://$VC_DOMAIN/rtc`
through traefik TLS (livekit-client appends `/rtc` itself — the
`LIVEKIT_URL` passed to the room UI must be the bare `wss://$VC_DOMAIN`,
no path). TURN is not deployed (server has a public IP; clients connect
out). If restrictive client networks ever fail, enable LiveKit's embedded
TURN instead of adding coturn.

## Egress quirk (important on this VPS)

Containers have **no runtime egress** (TCP or UDP), so LiveKit's
`use_external_ip: true` (STUN) can never work here. The compose service sets
`use_external_ip: false` and pins `NODE_IP=$VC_PUBLIC_IP` (the CLI
`--node-ip` env form) so candidates advertise the real public IP.

## Token flow & security

* `vc-room` mints HS256 JWTs (`pyjwt`) with the `video` grant
  (`roomJoin`, `canPublish/Subscribe/PublishData`) — no LiveKit API calls,
  no runtime dependency on the SFU; it only shares
  `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` via compose env.
* **Stateless**: rooms auto-create on first join and vanish when empty;
  nothing on disk, nothing to back up (backup.sh needs no entry).
* Room names `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`; display names sanitized,
  identity = name + random suffix.
* Openness: anyone with a link can join any room (like the public booking
  page). Gate `vc-room` behind the portal session if that ever matters.
* `livekit-client@2.23.0` is **vendored at build time** (no CDN at runtime).

## openSME integration

* **Portal tile**: `VC_URL` env → "Meet" tile on the Rust portal
  (`portal/src/main.rs`, `vc_url` field).
* **Public Calendar**: calrs booking `location_value` already points at
  `$VC_URL`; guests land on the lobby and can start the room from the
  booking confirmation's join link.
* **Future**: Element Call / MatrixRTC can reuse this exact SFU
  (`wss://$VC_DOMAIN/rtc`) as its `livekit_service_url` — no new SFU needed.

## Deploy (live host)

```bash
# repo files -> host copy (compose dir is a plain copy, no git)
scp -r images/vc-room services/vc.yml  # vc.yml only if using the live overlay form
cd /opt/opensme-compose/compose
docker buildx build --network host --load -t vc-room:1.0.0 images/vc-room
docker pull livekit/livekit-server:v1.13.11
# .env: VC_DOMAIN VC_URL VC_PUBLIC_IP VC_TCP_PORT VC_UDP_PORT
#       LIVEKIT_API_KEY LIVEKIT_API_SECRET LIVEKIT_IMAGE_TAG VC_ROOM_IMAGE_TAG
COMPOSE_PROFILES="...,vc" docker compose ... up -d vc-livekit vc-room
```

The live host currently runs VC via a `services/vc.yml` overlay + a tiny
`portal/vc-url.yml` (adds `VC_URL` to the portal env) appended to the
19-file chain + `--profile vc` (mirroring the pre-adoption calendar
pattern); the canonical home of the definitions is core
docker-compose.yml. Verified live end-to-end on 2026-11-07 with
`/tmp/vc_twoparty.py` (two headless WebRTC clients, both render two
tiles with two videos) via:

* `GET https://$VC_DOMAIN/` → lobby 200
* `GET /rtc/validate?access_token=<minted>` → 200 (signaling + token auth;
  the query param is `access_token`, NOT `token`)
* external `TCP 7881` reachable; UDP mux bound on the public IP
* `Permissions-Policy` on the room page: `camera=(self), microphone=(self),
  display-capture=(self)` (not the blanket fleet policy)
* portal `/` → 12 tiles incl. "Meet" → `https://vc.home.opensme.graphwiz.ai`
* calrs booking link lands on the lobby

## calrs integration — one room per booking (patch 0003)

Event-type locations support the template `{uid}`: at booking time calrs
replaces it with the booking's room-safe uid (the uuid part of
`<uuid>@calrs`), so EVERY booking renders its OWN join URL
`https://$VC_DOMAIN/room/<uuid>` on the confirmation page, in the
confirmation email, and in the ICS `LOCATION:` written back to CalDAV
(and mirrored into the sogo6 subscription). Guests click straight into
their room — no copying/pasting URLs, no typing a room name in the lobby
(`vc-room` mints the join token per room; `<uuid>` matches its
`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` room rule).

Set it up in the calrs dashboard → Event Types → Edit: press **"Use
self-hosted video (one room per booking)"** (visible when `CALRS_VC_URL`
is set on the `calendar` service) — it fills the location field with
`{VC}/room/{uid}` in one click. Manual equivalent:

```
https://vc.home.opensme.graphwiz.ai/room/{uid}
```

Verified live (2026-11-09): booking shows `Location:
https://vc.home.opensme.graphwiz.ai/room/<uuid>`; opening it auto-joins
LiveKit 1.13.11 (console: `connection state changed: connecting ->`
`connected`, room `<uuid>`, mic/camera/share/leave controls); the same
URL is in the Radicale write-back (`REPORT .../dav/booking/main/` →
`LOCATION:https://vc.../room/...`) and in the sogo6 mirror.

## Booking emails now deliver (patch 0004 — SMTP TLS relay)

calrs sends booking/confirmation emails over STARTTLS to the compose
internal Stalwart relay (`stalwart:25`). Stalwart's SMTP listeners serve a
per-boot SELF-SIGNED dev certificate, which lettre's default verifier
rejected with `invalid peer certificate: UnknownIssuer` — every booking
email died at the TLS handshake. Patch 0004 (`images/calrs/patches/
0004-smtp-selfsigned-relay.patch`) makes calrs accept the internal relay's
certificate and hostname (composition-internal hop, same posture as SOGo's
IMAP hop), and only attaches SMTP credentials when the config carries a
username — so a bare relay config (`username=""`) sails through
unauthenticated.

Live wiring: `smtp_config` = `stalwart:25`, empty username, `from_email =
demo@opensme.local`; the calrs admin account's `booking_email` points at
`testuser@opensme.local` (a real mailbox — Stalwart auto-provisions
principals via the IdP), so **host notifications land in a real INBOX**.
Verified live (2026-11-09): booking → Stalwart `queue.queue-message` →
`delivery.completed` to `testuser@opensme.local`. Guest addresses outside
the local domain correctly bounce with `550 Relay not allowed` (the demo
relays no external mail).

The config lives in the `smtp_config` row of `calrs.db` (volume
`compose_calrs-data`) — no env var. Operational details, failure
symptoms and the `calrs.db` surgery (empty `password_enc` silently kills
all booking e-mail; restore from a `.bak` volume copy) are in
[`docs/calendar.md`](calendar.md) → *Management* / *Troubleshooting*.
