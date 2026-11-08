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
cd calrs && docker buildx build --network host -t calrs:1.6.0 .
```

The image is ALWAYS built from the pinned upstream tag at deploy time, never
from this subtree.
