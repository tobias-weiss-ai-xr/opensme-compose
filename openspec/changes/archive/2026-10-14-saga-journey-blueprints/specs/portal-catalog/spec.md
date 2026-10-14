## MODIFIED Requirements

### Requirement: Saga journeys stay executable

For every catalog saga the test suite SHALL provide a journey blueprint that
validates the participating services' public contracts and SHALL skip cleanly
when a participating service is not deployed. Each journey blueprint SHALL
probe the public API surface (HTTP endpoints, OIDC flows, BPMN process
start, document export/ingest) of its participating services — not merely
portal navigation edges — and SHALL report a skip (not a failure) when any
participating service is absent from the active compose chain.

The six catalog sagas and their participating services:

| Saga | Participating services | Public contract validated |
|------|------------------------|---------------------------|
| Onboarding | IdP, portal, OpenCloud, SOGo | OIDC login → portal session → first tile opens → SSO reuse into files and mail |
| Quote-to-cash | Invoice Ninja, Paperless-ngx | Invoice export endpoint answers; DMS ingest API answers; both behind auth |
| Leave request | Operaton (BPMN), mail | Vacation-request process starts; approval tasks resolve; notification sent |
| Support ticket | Ticketing (Nosdesk) | Ticket creation API accepts; confirmation retrievable by reference |
| Publishing | CMS (website) | CMS publish API accepts content; published URL is publicly reachable |
| Team knowledge | Notes (Impress) | Knowledge content creation API accepts; content retrievable by reference |

#### Scenario: Onboarding journey on a full deployment

- **WHEN** the E2E journey suite runs with IdP, portal, OpenCloud, and SOGo
  all deployed and healthy
- **THEN** the onboarding journey completes: OIDC authorization-code flow
  succeeds, the portal session is established, the first catalog tile opens,
  and SSO reuse into OpenCloud and SOGo succeeds without a second credential
  check

#### Scenario: Onboarding journey skips on partial deployment

- **WHEN** the E2E journey suite runs without OpenCloud or SOGo deployed
- **THEN** the onboarding journey is reported as skipped, not failed

#### Scenario: Quote-to-cash journey on a full deployment

- **WHEN** the E2E journey suite runs with Invoice Ninja and Paperless-ngx
  both deployed and healthy
- **THEN** the quote-to-cash journey completes: the invoice export endpoint
  answers (HTTP 401/403 without credentials, not 404), and the DMS ingest
  API endpoint answers (HTTP 401/403 without credentials, not 404)

#### Scenario: Quote-to-cash journey skips on partial deployment

- **WHEN** the E2E journey suite runs without the invoice service or without
  the archive service
- **THEN** the quote-to-cash journey is reported as skipped, not failed

#### Scenario: Leave-request journey on a full deployment

- **WHEN** the E2E journey suite runs with Operaton and mail deployed and
  healthy
- **THEN** the leave-request journey completes: the vacation-request BPMN
  process starts via the engine API, the approval task list is non-empty,
  and task completion triggers a notification

#### Scenario: Leave-request journey skips on partial deployment

- **WHEN** the E2E journey suite runs without Operaton deployed
- **THEN** the leave-request journey is reported as skipped, not failed

#### Scenario: Support-ticket journey on a full deployment

- **WHEN** the E2E journey suite runs with the ticketing service deployed
  and healthy
- **THEN** the support-ticket journey completes: a ticket is created via the
  ticketing API, a confirmation reference is returned, and the ticket is
  retrievable by that reference

#### Scenario: Support-ticket journey skips on partial deployment

- **WHEN** the E2E journey suite runs without the ticketing service deployed
- **THEN** the support-ticket journey is reported as skipped, not failed

#### Scenario: Publishing journey on a full deployment

- **WHEN** the E2E journey suite runs with the CMS service deployed and
  healthy
- **THEN** the publishing journey completes: content is published via the
  CMS API and the published URL is reachable without authentication

#### Scenario: Publishing journey skips on partial deployment

- **WHEN** the E2E journey suite runs without the CMS service deployed
- **THEN** the publishing journey is reported as skipped, not failed

#### Scenario: Team-knowledge journey on a full deployment

- **WHEN** the E2E journey suite runs with the Notes service deployed and
  healthy
- **THEN** the team-knowledge journey completes: knowledge content is
  created via the Notes API and is retrievable by reference

#### Scenario: Team-knowledge journey skips on partial deployment

- **WHEN** the E2E journey suite runs without the Notes service deployed
- **THEN** the team-knowledge journey is reported as skipped, not failed

#### Scenario: Saga smoke on a partial deployment

- **WHEN** the E2E journey suite runs against a deployment without the
  invoice service
- **THEN** the quote-to-cash journey is reported as skipped, not failed
