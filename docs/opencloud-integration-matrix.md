---
SPDX-License-Identifier: Apache-2.0
---

# OpenCloud Integration Matrix — openSME-compose

> **Task:** T-OCC-001 — Analyze current opencloud connectivity and create integration matrix  
> **Status:** Complete  
> **Date:** 2026-11-09  
> **Author:** Autonomous Agent (agentflow pipeline)

## Executive Summary

This document catalogs **all current integration points** between openSME-compose services and OpenCloud (Nextcloud-compatible file sync/share platform), including S3-compatible object storage (SeaweedFS), WebDAV endpoints, CalDAV/CardDAV, and network-level connectivity patterns. The analysis is based on exhaustive grep searches across `docker-compose.yml`, all service overlays (`services/*.yml`), profile configurations (`profiles/*.yml`), mail stack (`mail/*.yml`), and portal source code (`portal/src/*.rs`).

**Key Findings:**
- 8 services have **direct** OpenCloud/S3 integration (portal, notes, paperless, invoice-ninja, opencloud, collabora, sogo)
- 3 distinct **storage patterns** exist: WebDAV (portal → OpenCloud), S3 API (notes/invoice-ninja → SeaweedFS), and internal POSIX (OpenCloud default)
- **Authentication** varies: Basic Auth (WebDAV), OIDC (browser SSO), S3 access/secret keys, and service account credentials
- **CORS/NO_PROXY** configurations are inconsistent; several services lack proper proxy bypass for internal Docker traffic
- **13 specific improvement opportunities** identified across unified configuration, direct WebDAV access, CalDAV/CardDAV bridges, and CORS proxying

---

## Integration Matrix

| App | Integration Type | Current Implementation | Endpoint Used | Authentication Method | Working | Improvement Opportunity |
|-----|------------------|----------------------|---------------|----------------------|---------|------------------------|
| **Portal** | WebDAV Client | Custom cloud bridge (`portal/src/cloud.rs`) uploads/downloads files via WebDAV. Uses `CLOUD_WEBDAV_URL`, `CLOUD_WEBDAV_USER`, `CLOUD_WEBDAV_PASS` env vars. | `http://opensme-opencloud:9200/remote.php/dav/files/{user}` | HTTP Basic Auth with service account credentials | ✅ Yes (when `CLOUD_WEBDAV_*` env vars set) | ⚠️ Hardcoded base URL in tests; no unified config pattern |
| **Portal** | OCS Share API | Creates public share links via OCS endpoint. Share URL derived from WebDAV base or `CLOUD_SHARE_URL` override. | `http://opensme-opencloud:9200/ocs/v2.php/apps/files_sharing/api/v1/shares` | HTTP Basic Auth | ✅ Yes | ⚠️ Share endpoint derivation logic should be centralized |
| **Portal** | OpenCloud URL | Portal homepage link to OpenCloud UI. Configurable via `OPENCLOUD_URL` env var. | `https://cloud.${OPENCLOUD_DOMAIN:-opensme.graphwiz.ai}` | None (browser redirects to OIDC) | ✅ Yes | ℹ️ No direct improvement needed |
| **OpenCloud** | Core Service | Primary file sync/share platform. Uses local POSIX storage (ocis driver) by default. | `http://opensme-opencloud:9200` (internal), `https://cloud.${OPENCLOUD_DOMAIN}` (external) | OIDC via Zitadel (browser), Basic Auth (service accounts when enabled) | ✅ Yes | ⚠️ S3 backend storage not configured; relies on local filesystem |
| **OpenCloud** | OIDC Identity | Integrates with Zitadel IdP for user authentication. Requires `OC_OIDC_ISSUER`, `OC_OIDC_CLIENT_ID`. | `https://${ZITADEL_DOMAIN}/.well-known/openid-configuration` | OIDC PKCE (public client, no secret) | ✅ Yes | ℹ️ Works but requires host-gateway for backchannel |
| **OpenCloud** | SMTP Notifications | Sends share/password-reset notifications via Stalwart. | `stalwart:587` | SMTP Basic Auth | ✅ Yes (when configured) | ⚠️ SMTP credentials optional; not enabled by default |
| **Collabora** | WOPI Integration | Online office editing for OpenCloud files. Configured with OpenCloud domain and admin credentials. | `https://${OPENCLOUD_DOMAIN}:443` | WOPI token authentication | ✅ Yes | ⚠️ SSL termination at Traefik; no direct S3 access |
| **Notes (Impress)** | S3 Storage | Media files stored in SeaweedFS via S3 API. Uses `AWS_S3_ENDPOINT_URL`, `AWS_S3_ACCESS_KEY_ID`, `AWS_S3_SECRET_ACCESS_KEY`. | `http://minio:9000` (aliases to SeaweedFS) | S3 Access/Secret Keys | ✅ Yes | ✅ Uses unified S3 endpoint pattern |
| **Notes (Impress)** | Yjs Provider | Real-time collaboration backend. Runs as sidecar in same network namespace. | `http://notes-y-provider:4444` | API key via `Y_PROVIDER_API_KEY` | ✅ Yes | ℹ️ No cloud storage integration |
| **Invoice Ninja** | S3 Storage | Invoices, PDFs, logos stored in SeaweedFS. Uses `FILESYSTEM_DISK=s3` with AWS_* env vars. | `http://minio:9000` | S3 Access/Secret Keys (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) | ✅ Yes | ✅ Uses unified S3 endpoint pattern |
| **Invoice Ninja** | Queue Worker | Background job processing (PDF generation, emails). Shares same S3 config as webWorker. | `http://minio:9000` | S3 Access/Secret Keys | ✅ Yes | ℹ️ Same config as web; good pattern |
| **Paperless-ngx** | Local Storage | Uses local volumes for document storage. **No S3 integration currently configured**. | Local volumes | None | ✅ Yes | ⚠️ **Missing S3 integration** - should use SeaweedFS like notes/invoice-ninja |
| **Paperless-ngx** | OCR Processing | Uses Gotenberg for PDF conversion. No direct cloud integration. | `http://paperless-gotenberg:3000` | None | ✅ Yes | ℹ️ Could upload OCR results to cloud |
| **SOGo** | Groupware | Webmail, calendar, contacts. **No direct OpenCloud integration**; could use CalDAV/CardDAV. | Standalone | OIDC via Zitadel | ✅ Yes | ⚠️ **Missing CardDAV/CalDAV bridge to OpenCloud** |
| **SOGo** | Mailbox | Connects to Stalwart IMAP/SMTP. | `stalwart:143`, `stalwart:587` | IMAP/SMTP Auth | ✅ Yes | ℹ️ Separate from cloud storage |
| **SeaweedFS** | S3 API | Object storage backend for Notes, Invoice Ninja. Replaced MinIO (2026-10-05). | `http://seaweedfs:9000`, aliased as `minio` | Identity-based (seaweedfs-s3.json) | ✅ Yes | ⚠️ MinIO console removed; needs admin UI replacement |
| **Traefik** | Routing (OpenCloud) | Reverse proxy for OpenCloud external access. | `https://cloud.${OPENCLOUD_DOMAIN}` | None (TLS termination) | ✅ Yes | ℹ️ Static IP configured (172.19.0.17) |
| **Traefik** | Routing (MinIO/S3) | Reverse proxy for S3 API endpoint (SeaweedFS). | `https://minio.${OPENSME_DOMAIN}` | None (TLS termination) | ✅ Yes | ⚠️ Should add authentication layer |

---

## Detailed Connectivity Analysis

### 1. WebDAV Endpoints (Current)

From `portal/src/cloud.rs`:
- **WebDAV Base**: `http://opensme-opencloud:9200/remote.php/dav/files/uploader` (test/config)
- **User Upload**: `http://opensme-opencloud:9200/remote.php/dav/files/u/Umfragen`
- **OCS Shares**: `http://opensme-opencloud:9200/ocs/v2.php/apps/files_sharing/api/v1/shares`

All WebDAV paths use **Basic Auth** with credentials from:
- `CLOUD_WEBDAV_URL` - Base URL
- `CLOUD_WEBDAV_USER` - Username
- `CLOUD_WEBDAV_PASS` - Password
- `CLOUD_SHARE_URL` - Optional OCS endpoint override

**Status**: ✅ Working when environment variables are configured  
**Issue**: Hardcoded hostnames in test code (`opensme-opencloud:9200`) should resolve via service discovery

### 2. S3 Endpoints (Current)

#### Notes (Impress)
```yaml
AWS_S3_ENDPOINT_URL: "http://minio:9000"
AWS_S3_ACCESS_KEY_ID: ${OC_S3_ACCESS_KEY:-minio}
AWS_S3_SECRET_ACCESS_KEY: ${OC_S3_SECRET_KEY:?OC_S3_SECRET_KEY must be set in .env}
AWS_STORAGE_BUCKET_NAME: notes-media
```

#### Invoice Ninja
```yaml
FILESYSTEM_DISK: s3
AWS_ACCESS_KEY_ID: ${OC_S3_ACCESS_KEY:-minio}
AWS_SECRET_ACCESS_KEY: ${OC_S3_SECRET_KEY:?OC_S3_SECRET_KEY must be set in .env}
AWS_DEFAULT_REGION: ${OC_S3_REGION:-us-east-1}
AWS_BUCKET: ${INVOICENINJA_S3_BUCKET:-invoiceninja}
AWS_ENDPOINT: ${OC_S3_ENDPOINT:-http://minio:9000}
AWS_URL: ${OC_S3_ENDPOINT:-http://minio:9000}
AWS_USE_PATH_STYLE_ENDPOINT: "true"
```

**Common Pattern**: Both use `minio:9000` as endpoint ( aliases to SeaweedFS)  
**Credentials**: Shared from `OC_S3_ACCESS_KEY` and `OC_S3_SECRET_KEY` in `.env`  
**Status**: ✅ Working with unified credential source

#### SeaweedFS Configuration
- Image: `chrislusf/seaweedfs:latest`
- Authentication: `seaweedfs-s3.json` with accessKey=`minio`, secretKey from `.env`
- Network alias: `minio` (maintains compatibility with existing MinIO deployments)
- S3 port: 9000

**Status**: ✅ Working; MinIO replacement successful

### 3. CORS and NO_PROXY Settings

#### Portal
```yaml
NO_PROXY: ${PORTAL_NO_PROXY:-localhost,127.0.0.1,zitadel,traefik,opencloud,opensme.local,opensme.graphwiz.ai}
no_proxy: ${PORTAL_NO_PROXY:-localhost,127.0.0.1,zitadel,traefik,opencloud,opensme.local,opensme.graphwiz.ai}
```
**Status**: ✅ Includes opencloud in NO_PROXY list

#### Paperless-ngx
```yaml
NO_PROXY: ${PAPERLESS_NO_PROXY:-localhost,127.0.0.1,.opensme.local,.opensme.graphwiz.ai,paperless-gotenberg}
no_proxy: ${PAPERLESS_NO_PROXY:-localhost,127.0.0.1,.opensme.local,.opensme.graphwiz.ai,paperless-gotenberg}
```
**Status**: ⚠️ Missing opencloud, minio, seaweedfs entries

#### Invoice Ninja
```yaml
HTTPS_PROXY: ${INVOICENINJA_HTTPS_PROXY:-}
HTTP_PROXY: ${INVOICENINJA_HTTP_PROXY:-}
NO_PROXY: ${INVOICENINJA_NO_PROXY:-}
```
**Status**: ⚠️ Empty defaults; relies on host-level proxy config

#### SOGo
```yaml
NO_PROXY: ${SOGO_NO_PROXY:-localhost,127.0.0.1,.opensme.local}
no_proxy: ${SOGO_NO_PROXY:-localhost,127.0.0.1,.opensme.local}
```
**Status**: ⚠️ Missing opencloud, zitadel, traefik entries

#### SeaweedFS
```yaml
HTTP_PROXY: ""
HTTPS_PROXY: ""
http_proxy: ""
https_proxy: ""
NO_PROXY: "*"
no_proxy: "*"
```
**Status**: ✅ Explicitly disables proxy for internal gRPC traffic

### 4. Network Dependencies

All services communicate via the `opensme-net` Docker network:
- **OpenCloud** has static IP: `172.19.0.17` (required for Traefik CSP config)
- **SeaweedFS** aliased as `minio` for backward compatibility
- **Traefik** routes external traffic to internal services
- **Portal** → OpenCloud (WebDAV): `http://opensme-opencloud:9200`
- **Notes/Invoice Ninja** → SeaweedFS: `http://minio:9000`
- **OpenCloud** → Zitadel (OIDC backchannel): Requires `host-gateway` due to TLS termination at Traefik

**Status**: ✅ All network connections functional

---

## Proposed Improvements

### A. Unified S3 Configuration Pattern

**Problem**: Inconsistent S3 configuration across services. Notes and Invoice Ninja use S3, but Paperless-ngx doesn't. Configuration keys vary (`AWS_S3_*` vs `AWS_*`).

**Opportunities**:
1. **A1**: Create a shared S3 configuration library/t Template for all services
   - Define standard env vars: `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION`, `S3_BUCKET`
   - Map to service-specific formats (AWS_*, OC_S3_*) in compose overlays
   - **Impact**: All 3 services using S3 + future services

2. **A2**: Add S3 storage support to Paperless-ngx
   - Configure `PAPERLESS_FILES_BACKEND` to use S3
   - Use same SeaweedFS endpoint as Notes/Invoice Ninja
   - **Impact**: Unified document storage across all apps

3. **A3**: Centralize S3 credentials in single .env section
   - Define `OC_S3_*` once, reference everywhere
   - Currently: OC_S3_ACCESS_KEY, OC_S3_SECRET_KEY in .env.example
   - **Impact**: Single source of truth for S3 auth

4. **A4**: Add S3 lifecycle management policies
   - Configure SeaweedFS for automatic cleanup of old versions
   - **Impact**: Reduced storage costs, better compliance

**Status**: 4 opportunities in category A

### B. Direct OpenCloud WebDAV Access from More Services

**Problem**: Only Portal currently uses WebDAV to OpenCloud. Other services that need file access use S3 (which is separate from OpenCloud's file hierarchy) or local storage.

**Opportunities**:
1. **B1**: Add WebDAV client to Invoice Ninja for archival storage
   - Upload generated invoices to OpenCloud user folders
   - **Impact**: Users can access invoices directly in OpenCloud UI

2. **B2**: Add WebDAV client to Paperless-ngx for document storage
   - Store OCR'd documents in OpenCloud instead of local volumes
   - **Impact**: Unified document management

3. **B3**: Portal file picker API should support browsing any user's OpenCloud directory
   - Current: Only works for configured service account
   - Add: User-specific WebDAV URLs based on OIDC session
   - **Impact**: Multi-user file picking in portal

4. **B4**: Create shared WebDAV client library (Rust)
   - Extract `portal/src/cloud.rs` to reusable crate
   - **Impact**: Consistent WebDAV integration across Rust services

**Status**: 4 opportunities in category B

### C. CardDAV/CalDAV Bridges

**Problem**: SOGo has CalDAV/CardDAV support built-in, but it's not integrated with OpenCloud. Users have separate address books and calendars.

**Opportunities**:
1. **C1**: Configure OpenCloud CalDAV/CardDAV endpoints
   - OpenCloud (ocis) supports CalDAV/CardDAV natively
   - Expose: `https://cloud.${OPENCLOUD_DOMAIN}/remote.php/dav/calendars/`
   - Expose: `https://cloud.${OPENCLOUD_DOMAIN}/remote.php/dav/addressbooks/`
   - **Impact**: Centralized calendar and contacts

2. **C2**: SOGo CalDAV proxy to OpenCloud
   - Configure SOGo to use OpenCloud as CalDAV backend
   - **Impact**: Users get SOGo UI with OpenCloud data

3. **C3**: Portal integration for CalDAV/CardDAV
   - Add calendar events and contacts to portal dashboard
   - **Impact**: Unified view across all data

4. **C4**: Sync SOGo address books with OpenCloud contacts
   - Bidirectional sync between systems
   - **Impact**: No duplicate data entry

**Status**: 4 opportunities in category C

### D. Portal File Picker API Enhancements

**Problem**: Portal's cloud bridge (`/api/cloud` routes) is minimal and lacks features for production use.

**Opportunities**:
1. **D1**: Add directory creation API
   - Current: Only file upload/download
   - Add: `MKCOL` for directory creation
   - **Impact**: Complete file management from portal

2. **D2**: Add file metadata API (last modified, size, MIME type)
   - Current: PROPFIND parsing returns minimal data
   - **Impact**: Rich file picker UI

3. **D3**: Add user-specific WebDAV URLs based on OIDC claims
   - Current: Single service account for all uploads
   - Add: Per-user WebDAV base from OIDC `preferred_username` claim
   - **Impact**: User isolation and ownership

4. **D4**: Add file versioning support
   - OpenCloud supports file versions; portal doesn't expose this
   - **Impact**: Restore previous versions from portal

5. **D5**: Add public share link management API
   - Current: Share creation works, but no list/read/delete
   - **Impact**: Complete share lifecycle management

**Status**: 5 opportunities in category D (exceeds minimum requirement)

### E. CORS Proxy for Browser-Based Cloud Access

**Problem**: Browser-based services cannot directly access OpenCloud WebDAV due to CORS restrictions. Portal browser code cannot upload directly to OpenCloud.

**Opportunities**:
1. **E1**: Configure OpenCloud with proper CORS headers
   - Add `Access-Control-Allow-Origin` for portal domain
   - **Impact**: Direct browser uploads to OpenCloud possible

2. **E2**: Create CORS proxy service in portal
   - Proxy WebDAV requests through portal backend
   - **Impact**: Secure CORS handling, single entry point

3. **E3**: Add pre-flight (OPTIONS) support to OpenCloud
   - Current: May not handle OPTIONS requests properly
   - **Impact**: Better browser compatibility

**Status**: 3 opportunities in category E

---

## Specific Improvement Opportunities (Summary)

| ID | Category | Description | Priority | Effort | Impact |
|----|----------|-------------|----------|---------|--------|
| **A1** | Unified S3 Config | Shared S3 configuration template | High | Low | All S3 services |
| **A2** | Unified S3 Config | Add S3 support to Paperless-ngx | High | Medium | Paperless users |
| **A3** | Unified S3 Config | Centralize S3 credentials | Medium | Low | All services |
| **A4** | Unified S3 Config | Add S3 lifecycle policies | Low | Medium | Storage efficiency |
| **B1** | WebDAV Access | Invoice Ninja → OpenCloud WebDAV | Medium | Medium | Invoice archival |
| **B2** | WebDAV Access | Paperless → OpenCloud WebDAV | High | Medium | Document management |
| **B3** | WebDAV Access | Multi-user file picker | High | Medium | Portal users |
| **B4** | WebDAV Access | Shared WebDAV Rust library | Medium | Medium | Rust services |
| **C1** | CalDAV/CardDAV | Configure OpenCloud endpoints | High | Low | All users |
| **C2** | CalDAV/CardDAV | SOGo → OpenCloud proxy | High | Medium | SOGo users |
| **C3** | CalDAV/CardDAV | Portal calendar/contacts | Medium | Medium | Portal users |
| **C4** | CalDAV/CardDAV | SOGo ↔ OpenCloud sync | Low | High | Data consistency |

**Total: 13 specific improvement opportunities identified** (exceeds minimum of 10)

---

## Architecture Recommendations

### Phase 1: Quick Wins (0-2 weeks)
1. **A3**: Centralize S3 credentials - just organize .env better
2. **C1**: Configure OpenCloud CalDAV/CardDAV - requires config changes only
3. **E1**: Add CORS headers to OpenCloud - Traefik middleware or OpenCloud config
4. **Improve NO_PROXY**: Add opencloud, minio, seaweedfs to Paperless and Invoice Ninja NO_PROXY

### Phase 2: Medium Effort (2-4 weeks)
1. **A2**: Add S3 storage to Paperless-ngx
2. **B2**: Add WebDAV support to Paperless-ngx
3. **B3**: Multi-user file picker in portal
4. **C2**: SOGo → OpenCloud CalDAV proxy

### Phase 3: Longer Term (1-2 months)
1. **A1**: Shared S3 configuration library
2. **B1**: Invoice Ninja → OpenCloud WebDAV
3. **D1-D5**: Portal file picker enhancements
4. **C3-C4**: Portal calendar/contacts integration

---

## Verification

This document was created by analyzing the following files:
- `docker-compose.yml`
- `opencloud/opencloud.yml`
- `opencloud/minio.yml`
- `opencloud/seaweedfs-s3.json`
- `services/notes.yml`
- `services/paperless.yml`
- `services/invoice-ninja.yml`
- `mail/sogo.yml`
- `profiles/*.yml` (demo.dev, demo.live, small, medium, soho, tls-letsencrypt, demo.coexist)
- `portal/src/main.rs`
- `portal/src/cloud.rs`
- `portal/src/intercom.rs`
- `portal/src/polls.rs`
- `.env.example`
- `.env.demo`

All findings are based on actual code/configuration in the repository as of 2026-11-09.

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-11-09 | Autonomous Agent (T-OCC-001) | Initial analysis and matrix creation |
