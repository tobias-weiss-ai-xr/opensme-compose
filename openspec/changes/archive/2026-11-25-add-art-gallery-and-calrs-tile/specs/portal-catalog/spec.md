# portal-catalog Delta

## MODIFIED Requirements

### Requirement: Function catalog completeness

The portal function catalog SHALL cover exactly the end-user business
functions of the suite — files (OpenCloud), mail (SOGo webmail), workflow
(Operaton), invoicing (Invoice Ninja), archive/DMS (Paperless-ngx), support
(ticketing), website (CMS), shop (store), notes (Impress), chat
(Element/Synapse), the AI assistant, calendar/booking (CalRS) and video
meetings (VC/LiveKit) — and each function SHALL render a tile/card on the
landing page if and only if its service URL is configured.

#### Scenario: Deployed function becomes visible

- **WHEN** a catalog function's service is deployed and its URL env var is
  non-empty (e.g. `INVOICE_URL`, `CALENDAR_URL`, `VC_URL`)
- **THEN** the landing page contains that function's tile linking to the
  configured URL

#### Scenario: Undeployed function stays hidden

- **WHEN** a catalog function's URL env var is empty or unset
- **THEN** no tile for that function is rendered

#### Scenario: CalRS tile with configured booking URL

- **WHEN** `CALENDAR_URL` is set to `https://calendar.<domain>`
- **THEN** the landing page renders the Calendar tile linking to that URL

## ADDED Requirements

### Requirement: Web frontend catalog parity

The Next.js frontend catalog (`portal/web/src/lib/config.ts`,
`buildServices`/`loadServiceSpecs`) SHALL mirror the catalog in
`contracts/tiles.yml`: every cataloged tile id SHALL have a spec entry and
an order entry, and tile names SHALL match the contract. The static
validation SHALL enforce this parity so the rendering frontend cannot drift
from the contract (2026-11-19: calendar and vc were absent from the web
catalog while contract, Rust catalog and compose env were complete).

#### Scenario: Web catalog missing a contract tile fails validation

- **WHEN** the static validation runs and `contracts/tiles.yml` catalogs
  `calendar` but `buildServices` has no calendar entry
- **THEN** the tiles check reports a violation

#### Scenario: Parity holds after the fix

- **WHEN** the static validation runs with calendar and vc present in both
  `contracts/tiles.yml` and the web catalog
- **THEN** the tiles check passes
