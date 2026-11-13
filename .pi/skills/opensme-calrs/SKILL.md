---
name: opensme-calrs
description: CalRS booking system ops for openSME — patch management (0001-0005), SMTP delivery via internal Stalwart relay, per-booking LiveKit room URL {uid} resolution, Radicale CalDAV backend, and build/deploy recipes.
---

# OpenSME CalRS Operations

Operational knowledge for calrs (olivierlambert/calrs v1.6.0) in openSME-compose.

## Core Contract
- **Image**: calrs:1.6.0-livekit (digest e25ef57dd548)
- **Patches**: 0001-0005 in images/calrs/patches/
- **Backend**: Radicale 3.9.0.0 CalDAV, dedicated (not SOGo)
- **Edge**: calendar.<domain> port 3000, /dav PathPrefix to Radicale
- **Booking URL**: https://calendar.<domain>/u/demo/vc (verified live; `/u/testuser/vc` returns "Event type not found" — testuser has no event types)
- **Admin**: https://calendar.<domain>/dashboard (demo@opensme.local)
- **VC**: https://vc.<domain>/room/{uid} per booking

## Patch Summary
| Patch | Fix | Commit |
|-------|-----|--------|
| 0001 | disable ICS import | baseline |
| 0002 | Radicale resourcetype calendar-home-set | 2c3e98d |
| 0003 | resolve {uid} in guest_reschedule_booking + templates | 68af4d8 |
| 0004 | accept self-signed certs + no AUTH when username empty | 29e6c5c |
| 0005 | branding footer "Powered by calrs & openSME" in templates/base.html | 4ae4791 |

## Build & Deploy
```bash
cd "${CALRS_DIR:-$HOME/build/calrs}"
git clone https://github.com/olivierlambert/calrs --branch v1.6.0 --depth 1 src
for p in 0001 0002 0003 0004 0005; do git -C src apply ../patches/${p}-*.patch; done
docker buildx build --network host -t calrs:1.6.0-livekit .
cd /opt/opensme-compose/compose
docker compose up -d --no-deps --force-recreate calendar
```
ALWAYS use buildx + --network host. Plain `docker build` FAILS with runc EOF on VPS.

## SMTP via Stalwart (patch 0004)
- Stalwart uses self-signed rcgen cert → UnknownIssuer without accept_invalid
- Empty username = no SMTP AUTH (fixes spurious AUTH attempts)
- Config in calrs.db: smtp_config (stalwart:25, use_starttls=1), booking_email=testuser@opensme.local
- Verify: `docker exec opensme-stalwart jsk queue list --limit 5` → delivery.completed

## {uid} Room URL Resolution (patch 0003)
- guest_reschedule_booking leaked literal {uid} in 4 sites (emails + CalDAV)
- Fix: let resolved_loc = loc_value.as_ref().map(|v| resolve_location(v, &uid))
- Unit test: reschedule_resolved_loc_maps_optional_location
- Suite: 543/0 after patch

## Radicale Store Gotchas
- /booking/ = calendar-home-set, actual calendars at /booking/main/ (child MKCALENDAR)
- resourcetype must include calendar-home-set (patch 0002)
