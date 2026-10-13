# portal-catalog Specification

## Purpose
Defines the portal as the SME function catalog: the set of end-user business
functions the suite exposes, how each becomes visible on the landing page
(function tiles / cards), the gating contract that ties a visible function to
a deployed, routed, healthy service, and the exclusion list of infrastructure
surfaces that must never appear as tiles.

## Requirements

### Requirement: Function catalog completeness

The portal function catalog SHALL cover exactly the end-user business
functions of the suite — files (OpenCloud), mail (SOGo webmail), workflow
(Operaton), invoicing (Invoice Ninja), archive/DMS (Paperless-ngx), support
(ticketing), website (CMS), shop (store), notes (Impress), chat
(Element/Synapse) and the AI assistant — and each function SHALL render a
tile/card on the landing page if and only if its service URL is configured.

#### Scenario: Deployed function becomes visible

- **WHEN** a catalog function's service is deployed and its URL env var is
  non-empty (e.g. `INVOICE_URL`)
- **THEN** the landing page contains that function's tile linking to the
  configured URL

#### Scenario: Undeployed function stays hidden

- **WHEN** a catalog function's URL env var is empty or unset
- **THEN** no tile for that function is rendered

### Requirement: Infrastructure exclusion

The portal SHALL NOT render function tiles for infrastructure surfaces:
identity/auth (IdP), mail backends (Stalwart admin), embedded editors
(Collabora), databases, caches, routing, or monitoring agents.

#### Scenario: Auth is never a tile

- **WHEN** the landing page is rendered regardless of `IDP_URL`
- **THEN** no Identity/auth tile is present

#### Scenario: Embedded editor is never a tile

- **WHEN** the landing page is rendered regardless of `COLLABORA_URL`
- **THEN** no Collabora tile is present; document editing is reached from
  within OpenCloud

### Requirement: Tile wiring contract

Every rendered function tile SHALL link to a service that is part of the
active deployment: the tile's URL env var SHALL map to a compose service in
the active chain that (a) carries Traefik routing labels for its domain and
(b) defines a healthcheck. A configured URL without a matching deployment is
a contract violation.

#### Scenario: Tile URL without deployment fails validation

- **WHEN** the static validation runs with `INVOICE_URL` set in the
  environment but no invoice service in the active compose chain
- **THEN** the tiles check reports a violation

#### Scenario: Deployed catalog service without tile fails validation

- **WHEN** the static validation runs and a user-facing service from the
  catalog is deployed in the active chain but its URL env var is absent from
  the portal environment mapping
- **THEN** the tiles check reports a violation

### Requirement: Saga journeys stay executable

For every catalog saga (onboarding, quote-to-cash, leave request, support
ticket, publishing, team knowledge) the test suite SHALL provide a journey
blueprint that validates the participating services' public contracts and
SHALL skip cleanly when a participating service is not deployed.

#### Scenario: Saga smoke on a partial deployment

- **WHEN** the E2E journey suite runs against a deployment without the
  invoice service
- **THEN** the quote-to-cash journey is reported as skipped, not failed
