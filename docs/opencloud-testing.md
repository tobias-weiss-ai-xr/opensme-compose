<!-- SPDX-License-Identifier: Apache-2.0 -->

# OpenCloud Integration Testing Guide

> Companion to [`docs/opencloud.md`](opencloud.md) (environment variables &
> integration patterns) and [`docs/opencloud-integration-matrix.md`](opencloud-integration-matrix.md)
> (connectivity matrix). This document is the **operator runbook** for
> verifying that OpenCloud (ocis) and its S3 (SeaweedFS), WebDAV, OCS,
> CalDAV, CardDAV and CORS surfaces are healthy — using only `curl` plus
> the standard openSME tooling.

OpenCloud (ocis) is fronted by Traefik at `https://${OPENCLOUD_DOMAIN}`
and listens internally on `opensme-opencloud:9200`. The examples below use
two variable sets:

- **Internal (from the compose host or another container on `opensme-net`):**
  `OC=http://opensme-opencloud:9200` — no TLS, no Traefik.
- **External (from a browser-facing client):**
  `OC=https://${OPENCLOUD_DOMAIN}` — Traefik TLS termination.

Service-account / Basic-auth credentials come from `.env`:

```bash
OC_USER="${CLOUD_WEBDAV_USER:-uploader}"      # or OC_ADMIN_USERNAME (admin)
OC_PASS="${CLOUD_WEBDAV_PASS:?set CLOUD_WEBDAV_PASS}"
```

> **Basic auth must be enabled** for service-account WebDAV/OCS calls:
> `OPENCLOUD_ENABLE_BASIC_AUTH=true` (see `docs/opencloud.md` §1.1, §1.2).

---

## 0. Prerequisites & sanity

```bash
# Bring the stack up (includes the minio overlay so the `minio` alias resolves)
docker compose --env-file .env up -d
docker compose --env-file .env ps opencloud seaweedfs

# Confirm the service is listening internally
docker exec opensme-opencloud sh -c 'wget -qO- http://localhost:9200/healthz || echo unhealthy'

# Resolve the env OpenCloud actually sees at runtime (T-OCC troubleshooting §3.10)
docker exec opensme-opencloud env | grep -E 'OC_|CLOUD_|OPENCLOUD_|NOTIFICATIONS_'
```

Conventions for the snippets below:

```bash
OC_INT=http://opensme-opencloud:9200          # internal
OC_EXT="https://${OPENCLOUD_DOMAIN:-cloud.opensme.graphwiz.ai}"   # external
AUTH="-u ${OC_USER}:${OC_PASS}"
H_OCS="-H 'OCS-APIREQUEST: true' -H 'Accept: application/json'"
```

> All `curl` examples assume `OPENCLOUD_ENABLE_BASIC_AUTH=true`. Browser SSO
> (OIDC PKCE) is **not** exercised here — these are machine/service-account
> tests against the DAV + OCS + S3 surfaces.

---

## 1. WebDAV tests (file storage)

WebDAV base for the service account:
`http://opensme-opencloud:9200/remote.php/dav/files/<user>`.

### 1.1 List a user's home (PROPFIND)

```bash
# PROPFIND depth:0 — verify the user root exists and is readable
curl -sS -X PROPFIND \
  -H 'Depth: 0' \
  -H 'Content-Type: application/xml' \
  ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/" \
  | xmllint --format -

# Expect: HTTP 207 Multi-Status; a <d:response> for the home collection
```

```bash
# PROPFIND depth:1 — list immediate children ( Depth: 1 )
curl -sS -i -X PROPFIND -H 'Depth: 1' ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/" | head -40
```

### 1.2 Upload a file (PUT)

```bash
# Upload a small test file (10 GiB cap: OC_UPLOAD_MAX_SIZE)
echo "opencloud webdav test $(date -Is)" > /tmp/oc-test.txt
curl -sS -i -X PUT \
  -H 'Content-Type: text/plain' \
  --data-binary @/tmp/oc-test.txt \
  ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-test.txt"
# Expect: HTTP 201 Created (first upload) or 204 No Content (overwrite)
```

### 1.3 Download it back (GET) and compare

```bash
curl -sS -o /tmp/oc-test.roundtrip.txt ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-test.txt"
diff -u /tmp/oc-test.txt /tmp/oc-test.roundtrip.txt && echo "roundtrip OK"
```

### 1.4 Stat / metadata (HEAD + PROPFIND on resource)

```bash
curl -sS -i -X HEAD ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-test.txt"
# Headers of interest: ETag, Last-Modified, Content-Length, Content-Type

# Full property set (quota, etag, fileid, permissions ...)
curl -sS -X PROPFIND -H 'Depth: 0' -H 'Content-Type: application/xml' \
  --data-binary @- ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-test.txt" <<'XML'
<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:displayname/><d:getcontentlength/><d:getetag/>
    <d:getlastmodified/><d:resourcetype/>
    <oc:fileid xmlns:oc="http://owncloud.org/ns"/>
    <oc:permissions xmlns:oc="http://owncloud.org/ns"/>
    <oc:share-types xmlns:oc="http://owncloud.org/ns"/>
  </d:prop>
</d:propfind>
XML
```

### 1.5 Create a folder (MKCOL) and delete (DELETE)

```bash
curl -sS -i -X MKCOL ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-test-dir"
# Expect: HTTP 201 Created

# Delete folder + uploaded file (cleanup)
curl -sS -i -X DELETE ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-test-dir"
curl -sS -i -X DELETE ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-test.txt"
```

### 1.6 Quota (PROPFIND on the root, `quota-available-bytes`)

```bash
curl -sS -X PROPFIND -H 'Depth: 0' -H 'Content-Type: application/xml' \
  --data-binary @- ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}" <<'XML'
<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:quota-available-bytes/>
    <d:quota-used-bytes/>
    <oc:quota xmlns:oc="http://owncloud.org/ns"/>
  </d:prop>
</d:propfind>
XML
```

---

## 2. OCS share tests

OCS shares endpoint:
`http://opensme-opencloud:9200/ocs/v2.php/apps/files_sharing/api/v1/shares`.
Always send `OCS-APIREQUEST: true` and prefer JSON (`Accept: application/json`).

### 2.1 List existing shares

```bash
curl -sS -i ${AUTH} ${H_OCS} \
  "${OC_INT}/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json"
# Expect: HTTP 200; ocs.meta.statuscode 200
```

### 2.2 Create a public share link for a file

```bash
# First upload a file to share (see §1.2)
echo "shared" > /tmp/share.txt
curl -sS -X PUT --data-binary @/tmp/share.txt ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/share.txt"

# Create a public (read-only) link
curl -sS -i -X POST ${AUTH} ${H_OCS} \
  --data-urlencode "path=/share.txt" \
  --data-urlencode "shareType=3"        \
  --data-urlencode "permissions=1"      \
  --data-urlencode "name=oc-test-share" \
  "${OC_INT}/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json"
# Expect: HTTP 200; response ocs.data.url = https://${OPENCLOUD_DOMAIN}/s/<token>
```

> If creation fails with *"public share must have a password"*, set
> `OPENCLOUD_PUBLIC_SHARE_PASSWORD=false` in `.env` (the poll-workflow
> setting, see `docs/opencloud.md` §3.7), or add
> `--data-urlencode "password=..."` and switch `permissions` as needed.

### 2.3 Add a password to a share (update)

```bash
SHARE_ID=42        # from the §2.2 response ocs.data.id
curl -sS -i -X PUT ${AUTH} ${H_OCS} \
  --data-urlencode "password=${OC_PASS}" \
  "${OC_INT}/ocs/v2.php/apps/files_sharing/api/v1/shares/${SHARE_ID}?format=json"
```

### 2.4 Delete a share

```bash
curl -sS -i -X DELETE ${AUTH} ${H_OCS} \
  "${OC_INT}/ocs/v2.php/apps/files_sharing/api/v1/shares/${SHARE_ID}?format=json"

# Cleanup the file
curl -sS -i -X DELETE ${AUTH} \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/share.txt"
```

### 2.5 Federated / user shares (shareType=1 / shareType=0)

```bash
# shareType=0  -> user share; shareType=1 -> group share; shareType=6 -> federated
curl -sS -i -X POST ${AUTH} ${H_OCS} \
  --data-urlencode "path=/share.txt" \
  --data-urlencode "shareType=0"      \
  --data-urlencode "shareWith=alice"  \
  --data-urlencode "permissions=19"   \
  "${OC_INT}/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json"
```

---

## 3. CalDAV tests (calendars)

ocis exposes CalDAV at `/remote.php/dav/calendars/<user>/`. The external
URL is `https://${OPENCLOUD_DOMAIN}/remote.php/dav/calendars/`.

### 3.1 Discover the current-user principal

```bash
curl -sS -X PROPFIND -H 'Depth: 0' -H 'Content-Type: application/xml' \
  --data-binary @- ${AUTH} "${OC_INT}/.well-known/caldav" <<'XML'
<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:current-user-principal/></d:prop>
</d:propfind>
XML
```

### 3.2 List calendars (calendar-home-set + PROPFIND)

```bash
PRINCIPAL="/principals/users/${OC_USER}"
curl -sS -X PROPFIND -H 'Depth: 0' -H 'Content-Type: application/xml' \
  --data-binary @- ${AUTH} "${OC_INT}${PRINCIPAL}" <<'XML'
<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:caldav="urn:ietf:params:xml:ns:caldav"
            xmlns:cs="http://calendarserver.org/ns/">
  <d:prop>
    <caldav:calendar-home-set/>
    <d:displayname/>
  </d:prop>
</d:propfind>
XML
```

```bash
# PROPFIND the calendar home (Depth: 1) to enumerate calendars
curl -sS -X PROPFIND -H 'Depth: 1' -H 'Content-Type: application/xml' \
  ${AUTH} "${OC_INT}/remote.php/dav/calendars/${OC_USER}/" | xmllint --format -
```

### 3.3 Create a calendar (MKCALENDAR)

```bash
curl -sS -i -X MKCALENDAR -H 'Content-Type: application/xml' \
  --data-binary @- ${AUTH} \
  "${OC_INT}/remote.php/dav/calendars/${OC_USER}/oc-test-cal" <<'XML'
<?xml version="1.0"?>
<caldav:mkcalendar xmlns:d="DAV:" xmlns:caldav="urn:ietf:params:xml:ns:caldav"
                   xmlns:ocs="http://owncloud.org/ns">
  <d:set>
    <d:prop>
      <d:displayname>OC Test Calendar</d:displayname>
      <caldav:supported-calendar-component-set>
        <caldav:comp name="VEVENT"/>
      </caldav:supported-calendar-component-set>
    </d:prop>
  </d:set>
</caldav:mkcalendar>
XML
# Expect: HTTP 201 Created
```

### 3.4 Create an event (PUT an iCalendar object)

```bash
curl -sS -i -X PUT -H 'Content-Type: text/calendar' \
  --data-binary @- ${AUTH} \
  "${OC_INT}/remote.php/dav/calendars/${OC_USER}/oc-test-cal/oc-test-event.ics" <<'ICS'
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//openSME//opencloud-test//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:oc-test-event-0001@opensme
DTSTAMP:20260101T120000Z
DTSTART:20260102T100000Z
DTEND:20260102T110000Z
SUMMARY:OpenCloud CalDAV test event
END:VEVENT
END:VCALENDAR
ICS
# Expect: HTTP 201 Created
```

### 3.5 Query events (REPORT / calendar-query)

```bash
curl -sS -i -X REPORT -H 'Depth: 1' -H 'Content-Type: application/xml' \
  --data-binary @- ${AUTH} \
  "${OC_INT}/remote.php/dav/calendars/${OC_USER}/oc-test-cal" <<'XML'
<?xml version="1.0"?>
<caldav:calendar-query xmlns:d="DAV:" xmlns:caldav="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:getetag/>
    <caldav:calendar-data/>
  </d:prop>
  <caldav:filter>
    <caldav:comp-filter name="VCALENDAR">
      <caldav:comp-filter name="VEVENT"/>
    </caldav:comp-filter>
  </caldav:filter>
</caldav:calendar-query>
XML
# Expect: HTTP 207 Multi-Status with the oc-test-event.ics payload
```

### 3.6 Cleanup

```bash
curl -sS -i -X DELETE ${AUTH} \
  "${OC_INT}/remote.php/dav/calendars/${OC_USER}/oc-test-cal/oc-test-event.ics"
curl -sS -i -X DELETE ${AUTH} \
  "${OC_INT}/remote.php/dav/calendars/${OC_USER}/oc-test-cal"
```

---

## 4. CardDAV tests (address books)

ocis exposes CardDAV at `/remote.php/dav/addressbooks/<user>/`.

### 4.1 Discover the CardDAV home (well-known)

```bash
curl -sS -X PROPFIND -H 'Depth: 0' -H 'Content-Type: application/xml' \
  --data-binary @- ${AUTH} "${OC_INT}/.well-known/carddav" <<'XML'
<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:current-user-principal/></d:prop>
</d:propfind>
XML
```

### 4.2 List address books

```bash
curl -sS -X PROPFIND -H 'Depth: 1' -H 'Content-Type: application/xml' \
  ${AUTH} "${OC_INT}/remote.php/dav/addressbooks/${OC_USER}/" | xmllint --format -
```

### 4.3 Create an address book (MKCOL with addressbook resource type)

```bash
curl -sS -i -X MKCOL -H 'Content-Type: application/xml' \
  --data-binary @- ${AUTH} \
  "${OC_INT}/remote.php/dav/addressbooks/${OC_USER}/oc-test-contacts" <<'XML'
<?xml version="1.0"?>
<d:mkcol xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:set>
    <d:prop>
      <d:displayname>OC Test Contacts</d:displayname>
      <card:addressbook-description>openSME CardDAV test</card:addressbook-description>
    </d:prop>
  </d:set>
</d:mkcol>
XML
# Expect: HTTP 201 Created
```

### 4.4 Create a contact (PUT a vCard)

```bash
curl -sS -i -X PUT -H 'Content-Type: text/vcard' \
  --data-binary @- ${AUTH} \
  "${OC_INT}/remote.php/dav/addressbooks/${OC_USER}/oc-test-contacts/oc-test-contact.vcf" <<'VCARD'
BEGIN:VCARD
VERSION:3.0
N:Doe;John;;;
FN:John Doe (OC test)
EMAIL;TYPE=INTERNET:john.doe@opensme.example.org
UID:oc-test-contact-0001
END:VCARD
VCARD
# Expect: HTTP 201 Created
```

### 4.5 Query contacts (REPORT / addressbook-query)

```bash
curl -sS -i -X REPORT -H 'Depth: 1' -H 'Content-Type: application/xml' \
  --data-binary @- ${AUTH} \
  "${OC_INT}/remote.php/dav/addressbooks/${OC_USER}/oc-test-contacts" <<'XML'
<?xml version="1.0"?>
<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop>
    <d:getetag/>
    <card:address-data/>
  </d:prop>
</card:addressbook-query>
XML
```

### 4.6 Cleanup

```bash
curl -sS -i -X DELETE ${AUTH} \
  "${OC_INT}/remote.php/dav/addressbooks/${OC_USER}/oc-test-contacts/oc-test-contact.vcf"
curl -sS -i -X DELETE ${AUTH} \
  "${OC_INT}/remote.php/dav/addressbooks/${OC_USER}/oc-test-contacts"
```

---

## 5. S3 tests (SeaweedFS, `minio:9000`)

SeaweedFS serves the S3 API on `:9000` and is network-aliased as `minio`
(requires the `opencloud/minio.yml` overlay). Credentials must match
`opencloud/seaweedfs-s3.json` and the `OC_S3_*` env vars.

```bash
S3_EP="${OC_S3_ENDPOINT:-http://minio:9000}"
S3_AK="${OC_S3_ACCESS_KEY:-minio}"
S3_SK="${OC_S3_SECRET_KEY:?set OC_S3_SECRET_KEY}"
BUCKET="${OC_S3_BUCKET:-opencloud}"
```

### 5.1 List buckets (raw S3 via curl + AWS Signature v4)

Raw `curl` against S3 requires a SigV4 signature. The simplest path is
`awscli`, but a self-contained curl example using `openssl` is shown below:

```bash
# SigV4 helper (POSIX sh, no deps beyond openssl + curl + sed)
s3_sig() {  # method path query payload
  local m="$1" p="$2" q="$3" pl="$4"
  local now date kdate kreg ksvc ksigh
  now="$(date -u +%Y%m%dT%H%M%SZ)"; date="${now%T*}"
  local hp="host:minio:9000\nx-amz-content-sha256:$(printf %s "$pl" | openssl dgst -sha256 -hex | awk '{print $2}')\nx-amz-date:${now}"
  local cr="$m\n$p\n$q\n${hp}\n\nhost;x-amz-content-sha256;x-amz-date\n$(printf %s "$pl" | openssl dgst -sha256 -hex | awk '{print $2}')"
  kdate=$(printf %s "$date" | openssl dgst -sha256 -mac HMAC -macopt hexkey:$(printf AWS4 | openssl dgst -sha256 -hex -mac HMAC -macopt hexkey:"$S3_SK" | awk '{print $2}') -hex | awk '{print $2}')
  kreg=$(printf "us-east-1" | openssl dgst -sha256 -mac HMAC -macopt hexkey:"$kdate" -hex | awk '{print $2}')
  ksvc=$(printf s3 | openssl dgst -sha256 -mac HMAC -macopt hexkey:"$kreg" -hex | awk '{print $2}')
  ksigh=$(printf "$date/us-east-1/s3/aws4_request" | openssl dgst -sha256 -mac HMAC -macopt hexkey:"$ksvc" -hex | awk '{print $2}')
  printf "%s" "$cr" | openssl dgst -sha256 -mac HMAC -macopt hexkey:"$ksigh" -hex | awk '{print $2}'
}

# GET service (list buckets) — empty body, empty query
SIG="$(s3_sig GET / '' '')"
curl -sS -i "http://minio:9000/" \
  -H "Host: minio:9000" \
  -H "x-amz-date: $(date -u +%Y%m%dT%H%M%SZ)" \
  -H "x-amz-content-sha256: $(printf '' | openssl dgst -sha256 -hex | awk '{print $2}')" \
  -H "Authorization: AWS4-HMAC-SHA256 Credential=${S3_AK}/$(date -u +%Y%m%d)/us-east-1/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=${SIG}"
# Expect: HTTP 200 + <ListAllMyBucketsResult>
```

### 5.2 Put / get / delete an object using `aws` CLI (recommended)

`awscli` is the pragmatic choice for S3 verification; the credentials above
already configure it via env:

```bash
export AWS_ACCESS_KEY_ID="$S3_AK"
export AWS_SECRET_ACCESS_KEY="$S3_SK"
export AWS_DEFAULT_REGION="${OC_S3_REGION:-us-east-1}"
export AWS_ENDPOINT_URL="$S3_EP"
# SeaweedFS speaks path-style HTTP (not HTTPS virtual-host)
aws_opts=(--endpoint-url "$S3_EP" --no-verify-ssl)

# List buckets
aws "${aws_opts[@]}" s3api list-buckets --query 'Buckets[].Name' --output table

# Create bucket (if missing) + put + get + delete
aws "${aws_opts[@]}" s3api create-bucket --bucket "$BUCKET" 2>/dev/null || true
echo "s3 test $(date -Is)" | aws "${aws_opts[@]}" s3 cp - "s3://${BUCKET}/oc-test/s3.txt"
aws "${aws_opts[@]}" s3 cp "s3://${BUCKET}/oc-test/s3.txt" -
aws "${aws_opts[@]}" s3 rm "s3://${BUCKET}/oc-test/s3.txt"
aws "${aws_opts[@]}" s3 ls "s3://${BUCKET}/oc-test/" 2>/dev/null
```

### 5.3 Health probe (no auth)

```bash
# SeaweedFS volume/cluster status (operator-only, no SigV4)
curl -sS -i "${S3_EP%/}/status"     # volume server status
curl -sS -i "${S3_EP%/}/cluster/status"
```

---

## 6. CORS tests

OpenCloud (ocis) emits `Access-Control-Allow-*` headers for browser apps.
The two CORS surfaces operators verify are:

1. **ocis DAV/OCS** at `${OC_EXT}` (Traefik → ocis `:9200`).
2. **SeaweedFS S3** at `${S3_EP}` (when a browser client, e.g. the portal,
   needs direct S3 access).

### 6.1 Preflight (`OPTIONS`) against the WebDAV endpoint

```bash
ORIGIN="https://${OPENSME_DOMAIN:-portal.opensme.graphwiz.ai}"
curl -sS -i -X OPTIONS \
  -H "Origin: ${ORIGIN}" \
  -H "Access-Control-Request-Method: PROPFIND" \
  -H "Access-Control-Request-Headers: authorization,depth,content-type" \
  "${OC_EXT}/remote.php/dav/files/${OC_USER}/"
# Expect: HTTP 200/204 with
#   Access-Control-Allow-Origin: <origin or *>
#   Access-Control-Allow-Methods: ... PROPFIND, GET, PUT, MKCOL, DELETE ...
#   Access-Control-Allow-Headers: authorization, depth, content-type
```

### 6.2 Preflight against the OCS shares endpoint

```bash
curl -sS -i -X OPTIONS \
  -H "Origin: ${ORIGIN}" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: authorization,ocs-apirequest,content-type" \
  "${OC_EXT}/ocs/v2.php/apps/files_sharing/api/v1/shares"
```

### 6.3 Real (credentialed) cross-origin request — confirm headers on a GET

```bash
curl -sS -i -X GET \
  -H "Origin: ${ORIGIN}" \
  ${AUTH} \
  "${OC_EXT}/remote.php/dav/files/${OC_USER}/" \
  | grep -iE '^Access-Control-Allow'
```

### 6.4 CORS against S3 (SeaweedFS)

SeaweedFS CORS is controlled by its config; verify the preflight reaches the
S3 API and returns the expected `Access-Control-Allow-Origin`:

```bash
curl -sS -i -X OPTIONS \
  -H "Origin: ${ORIGIN}" \
  -H "Access-Control-Request-Method: PUT" \
  -H "Access-Control-Request-Headers: authorization,content-type,x-amz-content-sha256,x-amz-date" \
  "${S3_EP}/${BUCKET}/oc-test/s3.txt"
```

If the browser app needs credentialed S3, ensure SeaweedFS is configured to
echo `Access-Control-Allow-Credentials: true` and a non-`*` origin.

---

## 7. Troubleshooting guide

| Symptom | Likely cause | Fix / check |
|---|---|---|
| WebDAV `PUT`/`PROPFIND` returns **401 Unauthorized** for the service account | Basic auth disabled, or wrong `CLOUD_WEBDAV_USER`/`CLOUD_WEBDAV_PASS` | Set `OPENCLOUD_ENABLE_BASIC_AUTH=true`; confirm the `uploader` (or admin) account exists in ocis IDM; `curl -i ${AUTH} ${OC_INT}/remote.php/dav/files/${OC_USER}/`. See `docs/opencloud.md` §1.2, §3. |
| WebDAV returns **404** for `/remote.php/dav/files/<user>` | Wrong user path; ocis uses the username as the path segment | Use the exact `OC_USER` (often `uploader`). The portal derives `CLOUD_WEBDAV_URL=http://opensme-opencloud:9200/remote.php/dav/files/uploader`. |
| OCS share creation fails with *"public link requires a password"* | `OC_SHARING_PUBLIC_SHARE_MUST_HAVE_PASSWORD=true` (default) | Set `OPENCLOUD_PUBLIC_SHARE_PASSWORD=false` for the poll workflow, or supply `password=`. See `docs/opencloud.md` §3.7. |
| CalDAV/CardDAV `MKCALENDAR`/`MKCOL` returns **403 Forbidden** | Calendars/Addressbooks app disabled, or service account lacks permissions | Verify the `graph`/`contacts`/`calendars` services are running (`docker compose ps opencloud`); ensure the account is a real ocis user (not just an OIDC shadow). |
| CalDAV `.well-known/caldav` redirects infinitely | Trailing-slash / host mismatch behind Traefik | Hit `${OC_EXT}/.well-known/caldav` (external) with `-L`; for internal tests use `${OC_INT}/.well-known/caldav` directly. |
| S3 `curl` returns **403 SignatureDoesNotMatch** | `OC_S3_ACCESS_KEY`/`OC_S3_SECRET_KEY` don't match `opencloud/seaweedfs-s3.json`, or host header mismatch | Compare `.env` `OC_S3_*` with `opencloud/seaweedfs-s3.json`; the `Host:` header in SigV4 **must** match what SeaweedFS expects (`minio:9000`). Restart `seaweedfs` after editing. See `docs/opencloud.md` §3.4. |
| `minio` hostname does not resolve from notes/invoice-ninja | The `opencloud/minio.yml` overlay (which defines the `minio` network alias) is not in `COMPOSE_FILE` | Add `:opencloud/minio.yml` to `COMPOSE_FILE`. See `docs/opencloud.md` §3.8. |
| CORS preflight returns **no** `Access-Control-Allow-*` headers | ocis/Traefik CORS not configured for the requesting origin, or request blocked at Traefik | Confirm the `Origin` matches an allowed site; check Traefik middleware labels on the `opencloud` router; for S3 check SeaweedFS `cors` config. |
| Browser SSO (OIDC) works but Basic-auth returns **401** | `PROXY_ENABLE_BASIC_AUTH` is `false` | Set `OPENCLOUD_ENABLE_BASIC_AUTH=true` (maps to `PROXY_ENABLE_BASIC_AUTH`). Basic-auth and OIDC coexist; the machine path needs the switch. |
| Notifications/share emails not delivered after a share is created | `NOTIFICATIONS_SMTP_*` misconfigured | Set `OC_SMTP_HOST/PORT/USERNAME/PASSWORD` and ensure a matching Stalwart account. See `docs/opencloud.md` §3.6. |
| IdP backchannel fails (`connection refused` on :443) | ocis reaches the public IdP hostname which resolves inside the net to the Zitadel container (no :443) | Keep `opencloud.yml`'s `${ZITADEL_DOMAIN}:host-gateway` extra_hosts. See `docs/opencloud.md` §3.9. |

### 7.1 Quick diagnostic one-liners

```bash
# Is ocis up and answering internally?
curl -sS -i "${OC_INT}/healthz"

# Is the external edge reachable?
curl -sS -i "${OC_EXT}/" | head -20

# Rendered env for opencloud (T-OCC §3.10)
docker compose --env-file .env config | sed -n '/services:/,/^volumes:/p' | grep -E 'OC_|CLOUD_|OPENCLOUD_|NOTIFICATIONS_'

# SeaweedFS S3 health
curl -sS "${OC_S3_ENDPOINT:-http://minio:9000}/status"

# Tail ocis logs while reproducing a failure
docker compose logs -f --tail=200 opencloud
```

### 7.2 End-to-end smoke script (WebDAV + OCS + S3)

```bash
#!/usr/bin/env sh
# scripts/opencloud-smoke.sh — minimal WebDAV + OCS + S3 round-trip
set -eu
OC_INT="${OC_INT:-http://opensme-opencloud:9200}"
OC_USER="${OC_USER:-uploader}"
OC_PASS="${OC_PASS:?set OC_PASS}"
S3_EP="${OC_S3_ENDPOINT:-http://minio:9000}"

echo "== WebDAV PUT/GET/DELETE =="
echo "smoke $(date -Is)" > /tmp/oc-smoke.txt
curl -fsS -X PUT --data-binary @/tmp/oc-smoke.txt \
  -u "${OC_USER}:${OC_PASS}" \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-smoke.txt"
curl -fsS -o /tmp/oc-smoke.rt -u "${OC_USER}:${OC_PASS}" \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-smoke.txt"
cmp /tmp/oc-smoke.txt /tmp/oc-smoke.rt
curl -fsS -X DELETE -u "${OC_USER}:${OC_PASS}" \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-smoke.txt"

echo "== OCS share create/delete =="
echo "sh" > /tmp/oc-share.txt
curl -fsS -X PUT --data-binary @/tmp/oc-share.txt -u "${OC_USER}:${OC_PASS}" \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-share.txt"
SID=$(curl -fsS -X POST -u "${OC_USER}:${OC_PASS}" \
  -H 'OCS-APIREQUEST: true' -H 'Accept: application/json' \
  --data-urlencode "path=/oc-share.txt" --data-urlencode "shareType=3" \
  --data-urlencode "permissions=1" --data-urlencode "name=smoke" \
  "${OC_INT}/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json" \
  | sed -n 's/.*"id":"\([0-9]*\)".*/\1/p' | head -1)
echo "created share id=${SID}"
curl -fsS -X DELETE -u "${OC_USER}:${OC_PASS}" \
  -H 'OCS-APIREQUEST: true' -H 'Accept: application/json' \
  "${OC_INT}/ocs/v2.php/apps/files_sharing/api/v1/shares/${SID}?format=json"
curl -fsS -X DELETE -u "${OC_USER}:${OC_PASS}" \
  "${OC_INT}/remote.php/dav/files/${OC_USER}/oc-share.txt"

echo "== S3 round-trip (awscli) =="
aws --endpoint-url "${S3_EP}" --no-verify-ssl \
   s3 cp - "s3://${OC_S3_BUCKET:-opencloud}/oc-smoke/s3.txt" <<< "s3 smoke"
aws --endpoint-url "${S3_EP}" --no-verify-ssl \
   s3 cp "s3://${OC_S3_BUCKET:-opencloud}/oc-smoke/s3.txt" -
aws --endpoint-url "${S3_EP}" --no-verify-ssl \
   s3 rm "s3://${OC_S3_BUCKET:-opencloud}/oc-smoke/s3.txt"

echo "ALL OK"
```

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-11-10 | Autonomous Agent (T-OCC3-009) | Initial integration testing guide: WebDAV, OCS shares, CalDAV, CardDAV, S3, CORS, troubleshooting runbook with curl examples. |
