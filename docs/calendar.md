# Public Calendar (calrs booking + dedicated Radicale CalDAV)

Status: adopted into the core stack (`docker-compose.yml` services
`calendar` + `calendar-radicale`, default-included — was an opt-in overlay
`services/calendar.yml`/profile `calendar`).

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

Operator deep-dive: the service definitions live in
[`docker-compose.yml`](../docker-compose.yml) (`calendar` + `calendar-radicale`);
[`services/calendar.yml`](../services/calendar.yml) is a deprecated empty shim.
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
> **Cancellation + CalDAV (ops):** `calrs booking cancel` (CLI) frees the
> slot but does NOT delete the already-pushed .ics from Radicale (only the
> web dashboard cancel does — it fires the CalDAV DELETE write-back). After
> CLI cancels, remove stale events with a CalDAV DELETE on
> `/booking/<uid>@calrs.ics`. The web dashboard cancel flow (login →
> `/dashboard/bookings` → POST `/dashboard/bookings/<id>/cancel`) is the
> only path that triggers write-back deletion; guest cancel tokens go by
> email (delivered via the internal Stalwart relay since patch 0004 — see
> the “Booking e-mail delivery” note below; a CLI-cancelled booking must
> still be cleaned by an explicit CalDAV DELETE on
> `/booking/<uid>@calrs.ics`).

> **CalDAV write-back (2026-11-08 fix):** calrs pushes bookings into the
> dedicated Radicale calendar only via the **web booking flow** (the
> dashboard cancel also writes back the DELETE). CLI `calrs booking create`
> sets status `confirmed` but skips write-back. Two fixes were needed to
> make write-back actually land:
>
> **Booking e-mail delivery (2026-11-09 fix):** confirmation, host-
> notification, guest cancel-token and reschedule e-mails now deliver.
> calrs historically failed every e-mail at the TLS handshake (lettre's
> `UnknownIssuer` against Stalwart's per-boot self-signed STARTTLS cert)
> — patch 0004 accepts the composition-internal relay cert and only
> attaches SMTP credentials when a username is set (a bare relay port
> advertises no AUTH). Live wiring: `stalwart:25`, empty username;
> host notifications go to `COALESCE(booking_email, email)` — on this
> stack the calrs admin's `booking_email` points at a real mailbox, so
> they land in an actual INBOX. Guest addresses outside the local domain
> correctly bounce `550 Relay not allowed` (the stack relays no external
> mail).
>
> 1. **`write_calendar_href` must be the calendar collection**
>    (`/booking/main/`), not the home (`/booking/`). calrs PUTs to
>    `{write_calendar_href}/{uid}.ics`; pointing at the home silently
>    dropped every booking. Fixed by repairing the href directly in
>    `calrs.db` (`calendars.write_calendar_href`) — there is no CLI or
>    dashboard setter for it.
>
> 2. **Radicale resourcetype parser** — Radicale renders the calendar
>    resourcetype as `<C:calendar />` (space before the self-closing
>    slash) and stock calrs v1.6.0's substring match misses it, so calrs
>    discovers zero calendars, syncs nothing, and write-back has no target
>    collection. Backport patch
>    `images/calrs/patches/0002-caldav-radicale-resourcetype.patch`
>    (upstream PR #135, commit 27a0e37, released in v1.14.0+) fixes the
>    parser to accept both `<C:calendar/>` and `<C:calendar />`.
>
> The sogo6 mirror (sogo6-server `1e60879`+) fetches via CalDAV REPORT
> (not ICS GET) — Radicale answers GET on a collection with 403, so sogo6
> falls back to REPORT `calendar-query` on the collection URL directly.
> An empty store mirrors as a valid empty calendar (subscription stays
> `completed` instead of flip-flopping to error).
>
> **Admin password caveat:** the pty feeder in `calendar-setup.sh` can
> mis-deliver the `calrs user create` password (rpassword reads the
> controlling tty) — if `demo@opensme.local` login fails, reset the
> Argon2 hash in `calrs.db` (`users.password_hash`) with a matching
> argon2id PHC string.

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
  `--app calendar` (sets `CALRS_OIDC_*`) plus the admin-panel enablement
  and the Zitadel compat patch — see step 6 in *Setup*.

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

## SOGo6 ↔ calrs: testuser's groupware calendar connected to the booking store

`scripts/provision-sogo-caldav.sh` (idempotent) connects the demo user's SOGo6
calendar to calrs:

1. starts the sogo6 **agent worker** (`python -m app.agent.run` inside
   `opensme-sogo6-server`) — the image's CMD only runs gunicorn, so without
   it external-calendar sync jobs stay `pending` forever;
2. creates (or repoints) an **external CalDAV subscription** on
   `testuser@opensme.local` via the sogo6 API (OIDC login through Zitadel)
   pointing at `https://<CALENDAR_DOMAIN>/dav/booking/main/` (the
   **calendar collection**, not the bare `/booking/` home — REPORT on the
   home returns an empty multistatus even when child calendars have events)
   with the booking htpasswd credentials
   (`RADICALE_USER`/`RADICALE_PASSWORD`). Credentials are **create-only**
   in the sogo6 external-calendar API: `POST /external-calendars` carries
   `username`/`password`, but the PUT update schema is URL-only and
   silently drops them. When creds are stale or absent, the script repairs
   the `sync_config` jsonb row directly in sogo6's Postgres
   (`sogo6_calendar_calendars`) rather than calling PUT — API serializers
   never echo `sync_config`, so a URL-only comparison would always show
   “stale” and a PUT would silently wipe the credentials.
3. verifies the endpoint: PROPFIND → **207** with auth, **401** anonymous.

Why the edge URL: sogo6's `IcsFetcher` is **https-only and rejects private
IPs** (SSRF guard), so the internal `http://calendar-radicale:5232/` name is
not usable from a subscribing client. `calendar-radicale` is therefore
exposed read-at-the-edge via a Traefik router `PathPrefix(`/dav`)` +
stripprefix (priority 10010, so it never shadows the calrs app router), with
Radicale's own htpasswd auth still on top. It is pinned to `172.19.0.250` on
`opensme-net` for internal IP-literal clients. **Privacy contract unchanged:
the store holds only public bookings — never point calrs or this route at a
staff/SOGo calendar.**

Mirror fetch: sogo6 (since sogo6-server `1e60879`) uses a CalDAV REPORT
mirror, not ICS GET. The `IcsFetcher` first tries GET on the subscription
URL; Radicale answers GET on a collection with 403, so sogo6 falls back to
REPORT `calendar-query` (getetag + calendar-data, VEVENT filter) on the
collection URL directly (Radicale serves REPORT on the collection even when
GET is 403), with PROPFIND discovery only as a fallback for servers that
need a distinct calendar href; every returned event is stitched into a
single VCALENDAR (CRLF, `PRODID:-//sogo6//caldav-mirror//EN`). An empty
store mirrors as a valid empty calendar (the subscription stays `completed`
instead of flip-flopping to error), and HTTP 405 on REPORT falls back to the
legacy GET-of-discovered-href. The subscription needs the Radicale
credentials at create time (`POST /external-calendars` carries
`username`/`password`; the update schema is URL-only, so recreate rather
than repoint when creds change — the provision script repairs stale creds
directly in Postgres as noted above). The CalDAV edge (PROPFIND 207 with
booking auth, 401 anonymous) and the subscription presence are verified by
`tests/05-e2e/stories.py` **X4**; the full booking → CalDAV → mirror →
cancel round trip is verified by **X5**, and Epic C4–C6 verifies the
per-booking VC room-URL contract (confirmation page + joinable room +
cancel) for every event.

> **Listing CalDAV subscriptions (2026-11-09 fix):** `GET /external-calendars`
> in sogo6-server used to call the interface with the hardcoded filter
> `source_type="ics"`, so CalDAV rows like this booking mirror never
> appeared in the list (they stayed synced, but were invisible to the
> picker/API). It now passes `source_type=None` (no filter) — ICS **and**
> CalDAV subscriptions are listed (sogo6-server `f3b47c2`). This keeps the
> mirror visible and avoids “the subscription vanished” confusion in the
> sogo6 webmail calendar picker after a redeploy.

## Deploy on the openSME VPS

The repo on the VPS is `/opt/opensme-compose/compose`. The `calendar` and
`calendar-radicale` services are part of the **core stack** (default-
included since the 2026-11-07 adoption — no overlay, no `--profile`),
but deploying a *fresh* stack still needs the steps below once.

1. **Build the calrs image locally** (no registry image exists — the repo
   does **not** ship the calrs sources; `images/calrs/Dockerfile` is a
   pinned reference + the `curl` delta for the healthcheck). Build from the
   upstream `v1.6.0` checkout with host networking so the build can pull
   crates:

   ```bash
   git clone https://github.com/olivierlambert/calrs.git
   git -C calrs checkout v1.6.0
   git -C calrs apply images/calrs/patches/0001-oidc-zitadel-compat.patch
   git -C calrs apply images/calrs/patches/0002-caldav-radicale-resourcetype.patch
   git -C calrs apply images/calrs/patches/0003-livekit-room-url.patch
   git -C calrs apply images/calrs/patches/0004-smtp-selfsigned-relay.patch
   git -C calrs apply images/calrs/patches/0005-branding-footer.patch
   cd calrs && docker buildx build --network host -t calrs:1.6.0 .
   ```

   Apply the patches **in order 0001 → 0005** (each is a delta against the
   previous state). What they cover on top of stock v1.6.0:

   - **0001** — OIDC compat for Zitadel (multi-audience ID tokens + userinfo
     email/name fallback), see step 6 below.
   - **0002** — REQUIRED for the Radicale store: Radicale renders the
     calendar resourcetype as `<C:calendar />` (space before the
     self-closing slash) and stock calrs v1.6.0's substring match misses it —
     calrs then discovers zero calendars, syncs nothing and write-back has no
     target collection (the historical "empty booking store" symptom). The
     patch is the backport of upstream PR #135 (commit 27a0e37, released in
     calrs v1.14.0+).
   - **0003** — per-booking LiveKit room URLs: event-type locations may use
     the `{uid}` template (`https://…/room/{uid}`); every confirmed booking
     renders its OWN resolved join URL on the confirmation page, in
     confirmation/reschedule e-mails and in the ICS `LOCATION:` written
     back to CalDAV. See [`docs/vc.md`](vc.md).
   - **0004** — SMTP delivery against the compose-internal Stalwart relay
     (accepts the self-signed STARTTLS cert; credentials only when a
     username is set). See “Booking e-mail delivery” and
     [`docs/vc.md`](vc.md).
   - **0005** — branding footer: the default page footer becomes
     “Powered by calrs & openSME” (links to https://cal.rs and
     https://github.com/tobias-weiss-ai-xr/opensme-compose) plus the calrs
     crab emoji. Pure `templates/base.html` delta — no code, no migrations.

   The tag (`calrs:1.6.0`) must match `CALRS_IMAGE_TAG` in `.env`. (The
   live demo image is built as `calrs:1.6.0-livekit` — same patches, the
   tag suffix is a deploy-time choice; Watchtower is disabled for it.)

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
   | `CALRS_ADMIN_EMAIL` | `admin@example.org` | bootstrap admin; first registered user becomes admin; also lets the E2E suite (Epic C6) cancel its own test bookings |
   | `CALRS_ADMIN_PASSWORD` | *(set)* | bootstrap admin password; feeds the E2E self-cleanup (Epic C6) |
   | `CALRS_VC_URL` | `https://meet.example.org/` | template meeting link rendered as the booking location |
   | `CALRS_OIDC_ISSUER` | *(empty)* | passthrough only for calrs 1.6.0 — enablement is DB-driven (admin panel); see step 6 |
   | `CALRS_OIDC_CLIENT_ID` | *(empty)* | OIDC client id from `zitadel-client.sh --app calendar` |
   | `CALRS_OIDC_CLIENT_SECRET` | *(empty)* | OIDC client secret |

4. **Bring the two services up** (they are part of the main compose —
   no overlay file, no profile needed):

   ```bash
   cd /opt/opensme-compose/compose
   docker compose up -d calendar calendar-radicale
   ```

   > **Legacy note (adoption 2026-11-07):** chains that still reference
   > `services/calendar.yml` via `COMPOSE_FILE` or pass `--profile calendar`
   > continue to work — that file is now an empty compatibility shim that
   > merges nothing. You can drop both from your deploy config.

5. **Provision** — run the idempotent setup script, which seeds the
   Radicale htpasswd, creates the calrs admin, points calrs at its
   Radicale CalDAV source, runs a full sync, creates the public
   `Video Consultation` event type, and prints the booking URL:

   ```bash
   ./scripts/calendar-setup.sh
   ```

6. **Optional SSO (Zitadel OIDC)** — register an OIDC client for the calendar
   app and let the script wire the redirect URI into `.env`:

   ```bash
   ./scripts/zitadel-client.sh --app calendar
   ```

   Then set `CALRS_OIDC_ISSUER`/`CALRS_OIDC_CLIENT_ID`/`CALRS_OIDC_CLIENT_SECRET`
   in `.env` and recreate the `calendar` service.

   **calrs 1.6.0 reality check** (three things the env vars will NOT do):

   1. **Enablement is DB-driven.** calrs 1.6.0 stores the OIDC client in its
      SQLite `auth_config` singleton, not in env — the `CALRS_OIDC_*`
      passthrough is dead weight for stock 1.6.0. Enable SSO from the admin
      dashboard (Settings → OIDC) or `POST /dashboard/admin/oidc` (admin web
      session; the CSRF token comes from the `calrs_csrf` cookie — the admin
      OIDC form itself sends no token).
   2. **Backchannel egress.** `CoreProviderMetadata::discover_async` runs
      inside the container; on egress-blocked bridges add
      `extra_hosts: ["${ZITADEL_DOMAIN}:host-gateway"]` to the calendar
      service (same pattern as `services/paperless.yml`) so discovery/token
      calls reach the IdP via Traefik's valid cert.
   3. **Zitadel compat patch is REQUIRED** (stock 1.6.0 dies in the callback
      against Zitadel). Apply `images/calrs/patches/0001-oidc-zitadel-compat.patch`
      to the v1.6.0 checkout before building `calrs:1.6.0` — it accepts
      Zitadel's project-wide multi-audience ID tokens (`azp` still names the
      authorized client) and falls back to userinfo for email/name claims,
      which Zitadel v2 keeps out of ID tokens.

   The shipped stack demonstrably works: login page → SSO → Zitadel
   (PKCE + nonce) → callback → dashboard session with an auto-provisioned
   local user. `tests/05-e2e/stories.py` Epic K pins the contract (SSO
   affordance, 303 authorize redirect, redirect_uri, handshake cookies,
   CSRF state gate).

> **Watchtower:** the `calendar` and `calendar-radicale` services are
> labelled `com.centurylinklabs.watchtower.enable=false` — the locally
> built `calrs` image is never auto-replaced, and the pinned Radicale tag
> is not bumped automatically. Upgrade `CALRS_IMAGE_TAG` deliberately.

## Management

- **Create more event types** on `https://<CALENDAR_DOMAIN>/dashboard`
  (each event type gets its own `/u/<user>/<slug>` booking page).
- **Every booking gets its own meeting URL**: with patch 0003, an event
  type's location may be the `{uid}` template (`{VC}/room/{uid}`) — the
  dashboard event-type form has a **“Use self-hosted video (one room per
  booking)”** button that fills it in one click. calrs substitutes the
  booking's room-safe uid at booking time, so the confirmation page, the
  e-mails and the ICS `LOCATION:` all carry that booking's own join URL.
  See [`docs/vc.md`](vc.md).
- **SMTP / booking e-mail** lives in calrs' own config (no env var): the
  `smtp_config` row in `calrs.db` — on this stack `host = stalwart`,
  `port = 25`, `username = ''` (relay mode; an empty username keeps lettre
  from trying AUTH on a port that advertises none), `password_enc` = a
  valid AES-256-GCM blob (leaving it empty silently disables ALL booking
  e-mail, not just auth). Host notifications go to the admin's
  `booking_email` (`COALESCE(booking_email, email)` on the user row) —
  point it at a real mailbox so confirmations land in an INBOX. Seed the
  row with the calrs CLI or SQLite surgery; details under *Troubleshooting*.
- **OIDC SSO** is enabled from the dashboard / `auth_config` (calrs 1.6.0
  is DB-driven; env is only passthrough) — see step 6 in *Setup*.

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
- **Booking e-mail not arriving:** check calrs' SMTP config —
  `smtp_config` in `calrs.db` (`host`/`port` should be `stalwart`/`25`,
  `username` empty). Symptoms: `email delivery failed … UnknownIssuer` in
  `docker logs opensme-calendar` = stale image without patch 0004;
  *no e-mail attempts logged at all* = `password_enc` was emptied
  (`load_smtp_config` fails closed — restoring a valid encrypted blob
  from a backup is required). Host e-mails go to `booking_email` —
  without it they are sent to the admin's login e-mail (undeliverable if
  that mailbox doesn't exist). Backup the `compose_calrs-data` volume
  (`.bak` copy) before any `calrs.db` edit — see `docs/vc.md`.
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
