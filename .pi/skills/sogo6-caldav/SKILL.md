---
name: sogo6-caldav
description: SOGo6 CalDAV external calendar knowledge — endpoint fixes, IcsFetcher URL constraints, sogo6-ui API proxy, SQL user source.
---

# SOGo6 CalDAV Operations

## External Calendar API
- GET/POST/DELETE `/api/user/v1/external-calendars`
- **Bug fix** (f3b47c2): ApiCalendar.get() used source_type="ics" hardcoded → only ICS returned
- **Fix**: pass source_type=None to interface → both ICS and CalDAV returned
- **Parent bump**: openSME-compose@1565cba
- **Test**: tests/test_api/test_external_calendars_list.py (110 LOA), suite 180 passed

## Example Subscription (calrs)
```json
{
  "name": "CalRS Bookings",
  "source_type": "caldav",
  "url": "https://calendar.home.opensme.graphwiz.ai/dav/booking/main/",
  "credentials": {"username": "", "password": ""}
}
```

## IcsFetcher URL Validation (marshmallow fields.Url)
**REJECTS**: private IPs (172.19.0.1), TLD-less names (radicale:5232), HTTP when SOGO_ICS_USE_HTTPS=True
**Workaround**: edge-terminate via Traefik with public FQDN + HTTPS

## sogo6-ui API Proxy 502 Fix
**Error**: 502 "fetch failed" on POST/PUT/PATCH/DELETE
**Cause**: request.json() → JSON.stringify() changed byte count, but original Content-Length forwarded
**Fix**: use request.text() to preserve raw bytes, delete Content-Length header

## SQL User Source (No LDAP)
- Single PostgreSQL DB (opensme-sogo6-postgres, db sogo) as auth source
- US_SQL_USER_URL = '' in domain settings triggers SQL source
- Table sogo_user: uid, cn, mail, domain, password, enabled
