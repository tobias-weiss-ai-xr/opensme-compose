# sme-interoperability Specification (delta)

## Purpose
Defines the integration contracts between the catalog functions and their
services: how identity, mail, files, news, processes, and invoicing connect
so the sagas (onboarding, quote-to-cash, leave request, support, publishing,
knowledge) run across service boundaries without bespoke glue. Interoperability
is contract-first: every surface names the mechanism (protocol/env), the
provider and consumer, and degrades explicitly when a participant is absent.

## ADDED Requirements

### Requirement: Single sign-on interoperability

Every catalog service that exposes a user login SHALL authenticate users via
the stack's central IdP over OIDC, configured through the service's compose
environment (issuer/client id/secret from the IdM env namespace), and the
portal SHALL link users into services only after OIDC-established session;
the IdM implementation itself SHALL remain swappable (Zitadel ↔ Casdoor) at
the compose-file level.

#### Scenario: Catalog app login redirects to IdP

- **WHEN** an unauthenticated user opens a catalog service's UI
- **THEN** login is performed through the central IdP's OIDC flow, not a
  service-local user database

#### Scenario: IdM swap point

- **WHEN** the deployment switches the IdM compose file (Zitadel → Casdoor)
- **THEN** catalog services keep working with only environment changes —
  no portal or service code changes

### Requirement: Mail interoperability

Services that send mail (notifications, invoices, DMS ingest) SHALL deliver
through the stack's mail service via SMTP submission using env-configured
host/port/credentials; user-facing mail reading SHALL go through the webmail
UI, and no catalog service SHALL require an external mail provider.

#### Scenario: App mail delivery without external provider

- **WHEN** a catalog service sends a notification (e.g. invoice mail, cloud
  share notification) while the stack has no external SMTP relay configured
- **THEN** the message is delivered via the internal mail service submission
  endpoint

#### Scenario: Webmail is the only mail surface for users

- **WHEN** the landing page is rendered
- **THEN** exactly one mail tile exists (webmail) — backend/admin mail UIs
  are not exposed as catalog functions

### Requirement: File share interoperability

Portal features that reference cloud content (e.g. team-note attachments)
SHALL accept share links of the configured file-service URL shape only, and
the portal SHALL link out to them without proxying or caching file content.

#### Scenario: Share link attachment accepted

- **WHEN** a team note is posted with an attachment URL matching the
  configured cloud share shape (`https://<cloud-domain>/s/…`)
- **THEN** the note renders the link as an attachment that opens in the
  file service

#### Scenario: Non-share attachment URL rejected

- **WHEN** a team note is posted with an attachment URL that does not match
  the configured cloud share shape
- **THEN** the portal rejects it with a validation message

### Requirement: News feed interoperability

The portal SHALL render news columns by fetching machine-readable feeds
(HTTP/HTTPS) from configured URLs inside the deployment network, SHALL only
fetch http(s) schemes, and SHALL degrade to a harmless empty/notice state
when a feed is unreachable — the portal's availability never depends on a
feed source.

#### Scenario: CMS news appear in the portal

- **WHEN** the CMS is deployed and `PORTAL_FEEDS` references its feed URL
- **THEN** published news items appear in the portal's news column

#### Scenario: Feed source down

- **WHEN** a configured feed URL is unreachable at render/fetch time
- **THEN** the portal still serves the landing page and the affected column
  shows a graceful notice

### Requirement: Process interoperability

Business processes SHALL integrate REST-only with the BPM engine (per the
`bpm-engine` capability): process deployment happens from the stack
(bootstrap), portal links into the engine UI as the workflow catalog tile,
and business apps reference processes by key — no shared database between
apps and the engine.

#### Scenario: Deployed process is linked, not embedded

- **WHEN** the workflow function is deployed and `BPM_URL` is set
- **THEN** the portal exposes the engine UI as the Workflow tile and no
  portal-to-engine database connection exists

### Requirement: Document flow interoperability

The quote-to-cash chain (invoice → approval → archive) SHALL compose
existing service contracts — invoicing exports documents via its public
API/exports, approval runs as a BPMN process, and archiving accepts
documents via the DMS ingest interface — without custom point-to-point code
in the portal; the portal's role stays navigation and visibility.

#### Scenario: Invoice lands in the archive

- **WHEN** an invoice is finalized in the invoicing service on a deployment
  that also runs the DMS
- **THEN** the document can be archived in the DMS via documented
  service-to-service interfaces (import/API), and the saga smoke test
  verifies both endpoints' availability

### Requirement: Interop wiring is statically validatable

The integration surfaces above SHALL be declared in a machine-readable
interop contract (OIDC env namespace, SMTP submission target, feed URLs,
share-link shape, process keys) and validated by the static test layer:
declared consumers must have their provider wiring present in the compose
chain, and absence must be a clean, reported skip — never a broken deploy.

#### Scenario: Interop contract drift detected

- **WHEN** a catalog service declares mail sending but its compose
  environment lacks the SMTP submission wiring
- **THEN** the static validation reports the drift

#### Scenario: Partial deployment stays deployable

- **WHEN** a provider of an integration surface is not deployed (e.g. no
  DMS on a small install)
- **THEN** dependent consumers keep starting, and validation marks the
  saga step skipped rather than failed
