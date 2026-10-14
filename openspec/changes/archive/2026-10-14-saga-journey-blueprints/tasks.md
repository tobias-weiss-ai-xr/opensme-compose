## 1. Shared precondition harness

- [x] 1.1 Create `tests/05-e2e/saga_common.py` with `probe_portal(session, domain) -> (base_url, services)`, `service_deployed(services, catalog_id) -> bool`, and `probe_endpoint(session, url, expect_status=(200, 401, 403)) -> bool` — extracted from `saga_smoke.py` P1–P5 logic. Acceptance gate: `python3 -c "from saga_common import probe_portal, service_deployed, probe_endpoint"` succeeds from the `tests/05-e2e/` directory.
- [x] 1.2 Refactor `saga_smoke.py` to import and call `saga_common` functions instead of inlining portal-probe logic; keep `main()` and CLI signature unchanged. Acceptance gate: `python3 tests/05-e2e/saga_smoke.py --help` exits 0 and the P1–P5 output is identical to before the refactor (run before/after on the same stack or skip-when-unreachable output).

## 2. Onboarding journey (S1)

- [x] 2.1 Create `tests/05-e2e/saga_onboarding.py`: probe IdP, portal, OpenCloud, SOGo; skip if any absent; when all present validate OIDC discovery → portal `/health` → first tile HTTP 200 → OpenCloud + SOGo SSO reuse (auth-code redirect, not 404). Acceptance gate: `python3 tests/05-e2e/saga_onboarding.py` exits 0 on a full stack, exits 0 (skip) on a stack without OpenCloud.

## 3. Quote-to-cash journey (S2)

- [x] 3.1 Create `tests/05-e2e/saga_quote_to_cash.py`: probe Invoice Ninja and Paperless-ngx; skip if either absent; when both present validate the invoice export endpoint (401/403, not 404) and the DMS ingest endpoint (401/403, not 404). Acceptance gate: `python3 tests/05-e2e/saga_quote_to_cash.py` exits 0 on a stack with both services, exits 0 (skip) without Invoice Ninja.

## 4. Leave-request journey (S3)

- [x] 4.1 Create `tests/05-e2e/saga_leave_request.py`: probe Operaton (BPMN) and mail; skip if Operaton absent; when present validate the engine API (`/engine-rest/process-definition/key/opensme-vacation-request`) is reachable and the vacation-request BPMN is deployed. Acceptance gate: `python3 tests/05-e2e/saga_leave_request.py` exits 0 on a stack with Operaton, exits 0 (skip) without Operaton.

## 5. Support-ticket journey (S4)

- [x] 5.1 Create `tests/05-e2e/saga_support_ticket.py`: probe the ticketing service; skip if absent; when present validate the ticket creation API endpoint (401/403, not 404). Acceptance gate: `python3 tests/05-e2e/saga_support_ticket.py` exits 0 on a stack with ticketing, exits 0 (skip) without ticketing.

## 6. Publishing journey (S5)

- [x] 6.1 Create `tests/05-e2e/saga_publishing.py`: probe the CMS service; skip if absent; when present validate the CMS publish API (401/403, not 404) and that a published content URL is publicly reachable (200). Acceptance gate: `python3 tests/05-e2e/saga_publishing.py` exits 0 on a stack with CMS, exits 0 (skip) without CMS.

## 7. Team-knowledge journey (S6)

- [x] 7.1 Create `tests/05-e2e/saga_team_knowledge.py`: probe the Notes service; skip if absent; when present validate the Notes API create + retrieve endpoints (401/403, not 404). Acceptance gate: `python3 tests/05-e2e/saga_team_knowledge.py` exits 0 on a stack with Notes, exits 0 (skip) without Notes.

## 8. CI wiring

- [x] 8.1 Add a `saga` target to the Makefile that runs all six saga scripts sequentially, aggregating exit codes (one skip does not block the next). Acceptance gate: `make saga` runs all six and exits 0 when all pass or skip.
- [x] 8.2 Extend the `e2e` Makefile target to run `run.py` then `make saga` so the full E2E layer includes saga journeys. Acceptance gate: `make e2e` runs SSO flows then all six saga journeys.

## 9. Documentation

- [x] 9.1 Update `docs/E2E-JOURNEYS.md` with per-saga blueprint documentation: persona, participating services, steps, skip conditions, and the test file mapping for all six sagas. Acceptance gate: the doc lists all six sagas with their participating services and references the new `saga_*.py` files.

## 10. Validation

- [x] 10.1 Run `openspec validate --change saga-journey-blueprints` and confirm the change validates (spec delta, design, tasks all coherent). Acceptance gate: `openspec validate --change saga-journey-blueprints` exits 0.
- [x] 10.2 Run `python3 tests/run.py --static` and confirm the static gate is green (no new violations from the test-suite changes). Acceptance gate: `python3 tests/run.py --static` exits 0.
- [x] 10.3 Run `python3 tests/05-e2e/saga_smoke.py` and confirm the refactored navigation smoke produces identical output to before the harness extraction. Acceptance gate: output matches the pre-refactor baseline.
