# calrs image — pin note

UPSTREAM: https://github.com/olivierlambert/calrs
TAG:      v1.6.0
COMMIT:   4bc67bddcad91e06e4c8544c0f7089100ac7022c (verified via `git ls-remote`
          https://github.com/olivierlambert/calrs.git -> refs/tags/v1.6.0)

calrs publishes NO registry image; the project ships a multi-stage Dockerfile
(rust:slim-trixie builder -> debian:trixie-slim runtime) which we vendor at
`images/calrs/Dockerfile` with these deltas:

1. `curl` in the runtime stage so the compose healthcheck
   (`curl -s -o /dev/null --max-time 5 http://127.0.0.1:3000/`) can probe
   :3000 — calrs has no `/health` route and curl exits 0 on any HTTP status
   without `-f`.
2. `patches/0001-oidc-zitadel-compat.patch` — OIDC SSO works against Zitadel
   (required for the demo IdP; apply AFTER `git checkout v1.6.0`):
   - **multi-audience ID tokens**: Zitadel puts every sibling client of the
     project into the `aud` claim (project token sharing, `azp` still names
     the authorized client). openidconnect 4.0.1 rejects unlisted extra
     audiences by default, so the verifier is extended with
     `set_other_audience_verifier_fn(|_| true)` (client_id containment + azp
     checks remain enforced).
   - **userinfo fallback**: Zitadel v2 keeps profile claims (email/name) out
     of the ID token entirely; when the ID token has no email, calrs now
     fetches `/oauth/v2/userinfo` with the access token.
   - `OAuth2TokenResponse` import for `access_token()`.
3. `patches/0002-caldav-radicale-resourcetype.patch` — CalDAV calendar
   discovery against Radicale (required: the calrs CalDAV source points at
   our dedicated `calendar-radicale` store; apply AFTER 0001):
   - Radicale renders the calendar resourcetype as `<C:calendar />` — WITH a
     space before the self-closing slash. calrs v1.6.0's `parse_calendar_list`
     matched the brittle substring `contains("calendar/>")`, so PROPFIND
     depth:1 against Radicale parsed `calendar_count=0`: calrs discovered NO
     calendars, synced nothing, and write-back had no target collection
     (store stayed empty).
   - Backported from upstream PR #135 (commit 27a0e37, released in calrs
     v1.14.0+): replaces the substring check with `has_calendar_resourcetype()`
     which matches a `calendar` resourcetype under any (or no) prefix,
     self-closing or open, with or without the space, while rejecting
     lookalikes (`calendar-color`, `calendar-data`,
     `supported-calendar-component-set`, `addressbook`) and the principal
     collection.
   - Also updates `scripts/calendar-setup.sh`: after connecting the source it
     MKCALENDARs (creates) `/booking/main/` as a VCALENDAR collection in
     Radicale if absent and points `write_calendar_href` there, so the
     provisioning chain is self-recovering.
4. `patches/0003-livekit-room-url.patch` — per-booking LiveKit room URLs
   (required for "pick a meeting URL without copy-paste"; apply AFTER 0002):
   - Event-type locations may use the template `{uid}` (e.g.
     `https://vc.example/room/{uid}`). At booking time calrs replaces `{uid}`
     with the booking's room-safe uid (the uuid part of `<uuid>@calrs`; room
     names must match `[A-Za-z0-9_-]`, no `@`) wherever a confirmed booking
     renders its location: the confirmed page, confirmation/reschedule
     emails, and the ICS `LOCATION:` line written back to CalDAV. So EVERY
     booking gets its OWN join URL — guests click straight into their room,
     nobody copies or pastes a URL.
     - `resolve_location()` helper + wiring in the three confirmed/booking
       handlers (`handle_booking_for_user`, `handle_group_booking`,
       `handle_dynamic_group_booking`). Slots/form pages show only the
       "Video call" label, so `{uid}` never leaks pre-booking.
   - The event-type form gets a "Use self-hosted video (one room per
     booking)" button (visible when `CALRS_VC_URL`/`VC_URL` is set) that
     fills the location field with `{VC}/room/{uid}` — one click, no
     copy-paste. `vc_url` is injected into all five event-type-form render
     contexts from `CALRS_VC_URL` (fallback `VC_URL`).
   - The guest/host RESCHEDULE path resolves `{uid}` too: the reschedule
     confirmation/pending emails and the pushed ICS `LOCATION:` use the
     booking's own resolved room URL instead of the literal template
     (`resolve_location(loc_value.as_ref().map(...))` in
     `guest_reschedule_booking`).
5. `patches/0004-smtp-selfsigned-relay.patch` — SMTP delivery works against a
   compose-internal Stalwart relay that serves a per-boot SELF-SIGNED dev
   certificate (apply AFTER 0003):
   - calrs used `AsyncSmtpTransport::starttls_relay`, whose default verifier
     rejects Stalwart's self-signed certificate with `invalid peer
     certificate: UnknownIssuer`, so every booking email failed at the TLS
     handshake. The patch builds the transport with `TlsParameters` accepting
     the local relay's certificate + hostname (composition-internal hop, same
     posture as SOGo's IMAP hop — no pinning, no trusted-CA requirement).
   - With credentials present, lettre also hard-fails with "No compatible
     authentication mechanism was found" on a bare relay port (25) that
     advertises no AUTH mechanism. Credentials are now added only when the
     SMTP config carries a non-empty username, so a config of
     `host=stalwart port=25 username=""` sends unauthenticated over STARTTLS
     into Stalwart's local relay.
   - Live wiring: `smtp_config` row `stalwart:25`, empty username, `from` =
     the calrs admin address; the booking account's `booking_email` points at
     a real mailbox (Stalwart auto-provisions principals via the IdP) so host
     notifications land in an actual INBOX. Guest addresses outside the
     local domain correctly bounce with `550 Relay not allowed` — the mail
     stack is a closed system by design, see [docs/mail.md](../../docs/mail.md).
6. `patches/0005-branding-footer.patch` — branding footer on every rendered
   page (apply AFTER 0004): diffs `templates/base.html` so the default
   footer becomes "Powered by calrs (https://cal.rs) & openSME
   (https://github.com/tobias-weiss-ai-xr/opensme-compose)" plus the calrs
   crab emoji.

## WARNING: this subtree does NOT self-build

The calrs source tree (Cargo.toml/Cargo.lock/src/migrations/assets/i18n/templates)
is NOT committed to openSME-compose. `images/calrs/Dockerfile` is therefore a
DOCUMENTED, PINNED reference, not a buildable recipe in this repo.

## Build (deploy time, from the UPSTREAM checkout)

```
git clone https://github.com/olivierlambert/calrs.git
git -C calrs checkout v1.6.0
git -C calrs apply images/calrs/patches/0001-oidc-zitadel-compat.patch
git -C calrs apply images/calrs/patches/0002-caldav-radicale-resourcetype.patch
git -C calrs apply images/calrs/patches/0003-livekit-room-url.patch
git -C calrs apply images/calrs/patches/0004-smtp-selfsigned-relay.patch
git -C calrs apply images/calrs/patches/0005-branding-footer.patch
cd calrs && docker buildx build --network host -t calrs:1.6.0 .
```

The image is ALWAYS built from the pinned upstream tag at deploy time, never
from this subtree.
