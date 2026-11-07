# Public Calendar (calrs booking + dedicated Radicale CalDAV)

Status: shipped (overlay `services/calendar.yml`, profile `calendar`).

This is the **public booking calendar** for openSME: a self-hosted
Cal.com-like booking page that lets external guests pick a free slot and
book a video-consultation session. It is built from two compose services:

- **calrs** — a self-hosted booking calendar written in Rust
  ([upstream](https://github.com/olivierlambert/calrs), pinned at `v1.6.0`),
  edge-routed at `https://<CALENDAR_DOMAIN>/` and backed by CalDAV for
  availability + bookings.
- **calendar-radicale** — a **dedicated** Radicale CalDAV store
  (`tomsquest/docker-radicale`) that lives **only** on the internal
  `opensme-net` network (no Traefik labels, never exposed at the edge).
  calrs reaches it at `http://calendar-radicale:5232/`.

> **Why a separate calendar?** The public booking page must never read from
> — or write to — staff/SOGo calendars. The Radicale store here holds
> **only** public bookings and is the single CalDAV source calrs is pointed
> at. See the [privacy contract](#privacy-contract) below.

Operator deep-dive for the compose overlay: [`services/calendar.yml`](../services/calendar.yml).
Image pin note + build recipe: [`images/calrs/README.md`](../images/calrs/README.md).
Provisioning script: [`scripts/calendar-setup.sh`](../scripts/calendar-setup.sh).
Also wired into the suite: a portal tile (contracts/tiles.yml → `CALENDAR_URL`) so staff
reach the booking dashboard from the portal, and the `calrs-data` / `radicale-config` /
`radicale-data` volumes are part of `scripts/backup.sh --volumes`.

> **Join-link requirement (2026-11-07 bug-hunt):** guests see a clickable
> "Location" link on the booking confirmation ONLY when the event type's
> `location_value` is set. calrs v1.6.0's CLI cannot set it (the VC URL rode in
> `--description`, which is never rendered to guests) — set it in the dashboard
> or seed `event_types.location_value` directly. Without it, a booked guest has
> no way to join the video call.
>
> **Cancellation + CalDAV (ops):** `calrs booking cancel` frees the slot but does
> NOT delete the already-pushed .ics from Radicale (only the web dashboard cancel
> does). After CLI cancels, remove stale events with a CalDAV DELETE on
> `/booking/<uid>@calrs.ics`.

> **CalDAV write-back dependency (2026-11-07 deploy):** calrs only pushes bookings into
> the dedicated Radicale calendar when an enabled `smtp_config` row exists — the push
> runs inside the SMTP-configured branch of the booking handler (even if SMTP delivery
> itself fails, the push still fires; seeded here via the dashboard settings or DB row,
> `host=stalwart`, `port=25`, `from=demo@opensme.local`). Without it bookings stay only
> in calrs's SQLite and Radicale holds no `.ics`. Admin password: the pty feeder in
> `calendar-setup.sh` can mis-deliver the `calrs user create` password (rpassword
> reads the controlling tty) — if `demo@opensme.local` login fails, reset the Argon2
> hash in `calrs.db` (`users.password_hash`) with a matching argon2id PHC string.

> **Live edge note (2026-11-07 deploy):** on the fleet traefik (`traefik_extra_routers`
> in `openedusuite.org.yaml`) the docker router labels are shadowed by a file router
> `calendar-sec` (priority 10000, `security-headers`, LE via the entrypoint's
> `mytlschallenge` resolver). The in-repo `certresolver=letsencrypt` label targets the
> repo-canonical compose traefik; on the fleet it only logs a harmless
> "nonexistent resolver" line. Same pattern as notes/store/surveys.

## URL & demo flow

| Page | URL | Who |
|---|---|---|
| Public booking page | `https://<CALENDAR_DOMAIN>/u/<admin-user>/vc` | guests (no login) |
| calrs dashboard | `https://<CALENDAR_DOMAIN>/dashboard` | staff (local-auth by default; optional Zitadel SSO) |

- `<CALENDAR_DOMAIN>` defaults to `calendar.opensme.graphwiz.ai` (set in
  `.env`); `<admin-user>` is the calrs admin's username — derived from the
  bootstrap admin e-mail's local part with `.` → `-`
  (`CALRS_ADMIN_EMAIL`), the same convention the setup script prints.
- A guest opens the booking page, sees only free/busy slots for the
  `vc` ("Video Consultation") event type, picks a slot, and books. calrs
  writes the booking into the Radicale source and renders the VC link
  (`CALRS_VC_URL`) as the meeting location.
- Staff sign in to `/dashboard` to manage event types, sources and the
  VC link. By default calrs uses **local authentication** (the first
  registered user is admin). Optional Zitadel SSO is wired with
  [`scripts/zitadel-client.sh`](../scripts/zitadel-client.sh)
  `--app calendar` (sets `CALRS_OIDC_*`).

## Privacy contract (important)

This is a hard user requirement, not a preference:

- **The public booking interface exposes only free/busy time slots** —
  never event titles, attendees, subjects, locations, or anything from a
  staff calendar. calrs renders its public booking pages free/busy-only by
  design.
- **Bookings live ONLY in the dedicated Radicale calendar.** calrs is
  pointed at one CalDAV source — `calendar-radicale` on `opensme-net` —
  and that source holds only public bookings. The
  `scripts/calendar-setup.sh` provisioner is what creates that source
  (`calrs source add` → `http://calendar-radicale:5232/`).
- **Never add a staff/SOGo calendar as a calDAV source** to calrs. SOGo
  calendars carry private event metadata (titles, attendees, subjects);
  surfacing them on a public booking page would leak it. The Radicale
  store here is intentionally separate from any groupware calendar.

If a deployment ever needs to *show* real staff availability on the
booking page, that is a new, separate feature — not "just point calrs at
SOGo" — and must preserve the free/busy-only contract above.

## Deploy on the openSME VPS

The repo on the VPS is `/opt/opensme-compose/compose`. The calendar overlay
is **opt-in** via the `calendar` profile.

1. **Build the calrs image locally** (no registry image exists — the repo
   does **not** ship the calrs sources; `images/calrs/Dockerfile` is a
   pinned reference + the `curl` delta for the healthcheck). Build from the
   upstream `v1.6.0` checkout with host networking so the build can pull
   crates:

   ```bash
   git clone https://github.com/olivierlambert/calrs.git
   git -C calrs checkout v1.6.0
   cd calrs && docker buildx build --network host -t calrs:1.6.0 .
   ```

   The tag (`calrs:1.6.0`) must match `CALRS_IMAGE_TAG` in `.env`.

2. **Pull the Radicale image** from Docker Hub (no build needed):

   ```bash
   docker pull tomsquest/docker-radicale:3.9.0.0
   ```

3. **Set the calendar env** in `.env` (placeholders only — see
   [`.env.example`](../.env.example)):

   | Variable | Example / default | Notes |
   |---|---|---|
   | `CALENDAR_DOMAIN` | `calendar.opensme.graphwiz.ai` | public host for the booking page + dashboard |
   | `CALRS_IMAGE_TAG` | `1.6.0` | must match the locally-built `calrs:` tag |
   | `RADICALE_IMAGE_TAG` | `3.9.0.0` | tomsquest/docker-radicale tag |
   | `RADICALE_USER` | *(set)* | booking CalDAV login, seeded into Radicale htpasswd |
   | `RADICALE_PASSWORD` | *(set)* | booking CalDAV password (bcrypt-hashed at rest) |
   | `RADICALE_AUTH_HTPASSWD_ENCRYPTION` | `bcrypt` | `bcrypt` (needs `htpasswd -B`) or `plain` |
   | `CALRS_ADMIN_EMAIL` | `admin@example.org` | bootstrap admin; first registered user becomes admin |
   | `CALRS_ADMIN_PASSWORD` | *(set)* | bootstrap admin password |
   | `CALRS_VC_URL` | `https://meet.example.org/` | template meeting link rendered as the booking location |
   | `CALRS_OIDC_ISSUER` | *(empty)* | blank = local-auth only; set for Zitadel SSO |
   | `CALRS_OIDC_CLIENT_ID` | *(empty)* | OIDC client id from `zitadel-client.sh --app calendar` |
   | `CALRS_OIDC_CLIENT_SECRET` | *(empty)* | OIDC client secret |

4. **Add the overlay and the profile**, then bring the two services up:

   ```bash
   cd /opt/opensme-compose/compose
   # add services/calendar.yml to your active COMPOSE_FILE chain, e.g.:
   export COMPOSE_FILE="docker-compose.yml:idm/zitadel.yml:services/calendar.yml"
   export COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES:}calendar"
   docker compose up -d calendar calendar-radicale
   ```

   (You can also start it with `docker compose --profile calendar up -d`.)

5. **Provision** — run the idempotent setup script, which seeds the
   Radicale htpasswd, creates the calrs admin, points calrs at its
   Radicale CalDAV source, runs a full sync, creates the public
   `Video Consultation` event type, and prints the booking URL:

   ```bash
   ./scripts/calendar-setup.sh
   ```

6. **Optional SSO** — register an OIDC client for the calendar app and let
   the script wire the redirect URI into `.env`:

   ```bash
   ./scripts/zitadel-client.sh --app calendar
   ```

   Then set `CALRS_OIDC_ISSUER`/`CALRS_OIDC_CLIENT_ID`/`CALRS_OIDC_CLIENT_SECRET`
   in `.env` and recreate the `calendar` service.

> **Watchtower:** the `calendar` and `calendar-radicale` services are
> labelled `com.centurylinklabs.watchtower.enable=false` — the locally
> built `calrs` image is never auto-replaced, and the pinned Radicale tag
> is not bumped automatically. Upgrade `CALRS_IMAGE_TAG` deliberately.

## Management

- **Create more event types** on `https://<CALENDAR_DOMAIN>/dashboard`
  (each event type gets its own `/u/<user>/<slug>` booking page).
- **Set / edit the VC link** per event type from the dashboard (the
  setup script puts the `CALRS_VC_URL` template into the `vc` event
  type's description).
- **SMTP / e-mail confirmations** and **OIDC SSO** are configured from
  the dashboard / `.env` as calrs supports them.

## Troubleshooting

- **calrs logs:** `docker logs opensme-calendar` (container name is
  `opensme-calendar`; compose service `calendar`).
- **Radicale logs:** `docker logs opensme-calendar-radicale` (compose
  service `calendar-radicale`).
- **htpasswd file:** the booking CalDAV login lives at `/config/users`
  inside the `radicale-config` volume, seeded by
  `scripts/calendar-setup.sh` via a one-shot `httpd:2.4-alpine` helper
  (the Radicale image ships no `htpasswd(1)`). Re-run the script to
  (re)seed it.
- **PROPFIND smoke test** as the booking user — verify the CalDAV store
  answers authenticated requests over `opensme-net`:

  ```bash
  docker compose exec calendar-radicale \
    curl -s -u "$RADICALE_USER:$RADICALE_PASSWORD" \
    -X PROPFIND -H 'Depth: 0' \
    -H 'Content-Type: application/xml' \
    --data '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>' \
    http://127.0.0.1:5232/
  ```

  A `207 Multi-Status` (or any authenticated response, not `401`) means
  the htpasswd login and the CalDAV store are healthy.
- **Booking page 404 / empty slots:** confirm the `vc` event type exists
  (`calrs event-type list`) and that `calrs sync` succeeded — the setup
  script runs `--full` on first source add.
