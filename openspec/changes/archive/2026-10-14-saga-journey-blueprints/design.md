## Context

The `portal-catalog` spec already requires journey blueprints for all six
catalog sagas. The implementation delivered only `saga_smoke.py`, which pins
navigation edges (portal health, tile reachability, feed degradation) — not
the per-saga public-contract validation the spec mandates. Five of six sagas
have no journey at all. See `proposal.md` for the gap analysis.

Existing conventions (from `saga_smoke.py`, `resilience.py`, `backup.py`):
each E2E journey is a **standalone script** with its own `main()` returning
`bool`, importing `Result` from `conftest` and `T`, `load_env_file`,
`is_localish`, `install_dns_fallback` from `run`. They probe a running stack
over HTTP, use `result.ok/fail/skip`, and are invoked directly
(`python3 tests/05-e2e/<name>.py [domain]`). `make e2e` currently runs only
`run.py` (SSO flows); the standalone journeys are not wired into CI.

Service-to-host mappings live in `contracts/tiles.yml`; cross-service
contracts (document_flow, processes, smtp_submission) live in
`contracts/interop.yml`. The `OPTIONAL_CATALOG` tuple in `saga_smoke.py`
already encodes which catalog functions are deployment-optional.

## Goals / Non-Goals

**Goals:**
- One journey blueprint per catalog saga (6 files) that validates the
  **public contracts** of its participating services, not just navigation.
- Clean skip when any participating service is absent (skip, not fail).
- Reuse the portal-probe logic from `saga_smoke.py` via a shared harness —
  no copy-paste of health/tile/feed checks.
- Wire the saga journeys into CI via a `make saga` target and extend
  `make e2e` to include them after SSO flows.

**Non-Goals:**
- No portal, compose, or service code changes — this is a test-suite
  completeness change.
- No browser automation — journeys use HTTP only (same convention as
  `run.py` and `saga_smoke.py`).
- No new contracts or contract changes — `tiles.yml` and `interop.yml`
  already define the service mappings and integration surfaces.
- No performance benchmarking of saga paths.

## Decisions

### D1: One file per saga (not one parametrized file)

Each saga gets its own file: `saga_onboarding.py`, `saga_quote_to_cash.py`,
`saga_leave_request.py`, `saga_support_ticket.py`, `saga_publishing.py`,
`saga_team_knowledge.py`. This matches the existing one-journey-per-file
convention (`resilience.py`, `backup.py`, `mailcow_journey.py`) and keeps
each saga's contract logic readable and independently runnable.

**Alternative considered:** a single `sagas.py` with a `--saga` flag. Rejected
because it diverges from the established convention and makes skip-per-saga
output less granular (one process, one result summary vs six independent
summaries).

### D2: Shared precondition harness (`saga_common.py`)

Extract `saga_smoke.py`'s P1–P5 logic (portal health, `/api/services` tile
discovery, `/api/feeds` degradation check, optional-catalog skip detection)
into `tests/05-e2e/saga_common.py`:

- `probe_portal(session, domain) -> (base_url, services_list)` — finds the
  portal, checks `/health`, returns the base URL and rendered tiles.
- `service_deployed(services, catalog_id) -> bool` — checks whether a
  catalog function's tile is rendered (reuses `OPTIONAL_CATALOG` logic).
- `probe_endpoint(session, url, expect_status=(200, 401, 403)) -> bool` —
  probes a service endpoint, treating auth-redirect (401/403) as "answers"
  and 404/connection-refused as "absent".

`saga_smoke.py` becomes a thin caller of `saga_common` (navigation-level
checks stay there; the harness provides the reusable primitives).

### D3: Skip semantics — probe before validate

Each saga journey:
1. Calls `probe_portal` to get the rendered tile set.
2. Checks each participating service via `service_deployed`.
3. If any required service is absent → `result.skip(...)` and return 0.
4. If all present → validate the saga's public contracts (see spec for
   per-saga contracts) → `result.ok/fail` per check.

This matches the spec's "skip cleanly when a participating service is not
deployed" and the existing `saga_smoke.py` P4 pattern.

### D4: Contract probing per saga

Each saga validates its public contracts via HTTP only (no browser):

| Saga | Contract probes |
|------|----------------|
| Onboarding | OIDC discovery → portal `/health` → first tile HTTP 200 → OpenCloud + SOGo SSO reuse (auth-code redirect, not 404) |
| Quote-to-cash | Invoice Ninja export endpoint (401/403, not 404) + Paperless ingest endpoint (401/403, not 404) |
| Leave request | Operaton engine API (`/engine-rest/process-definition/key/opensme-vacation-request`) reachable; BPMN deployed |
| Support ticket | Ticketing API creation endpoint (401/403, not 404) |
| Publishing | CMS publish API (401/403, not 404) + public URL reachable (200) |
| Team knowledge | Notes API create + retrieve (401/403, not 404) |

Auth-wall probes (401/403 = "endpoint exists and is protected") are the
public-contract signal — 404 or connection-refused means the service is
absent or misrouted, which is a fail (if deployed) or a skip (if not
deployed).

### D5: CI wiring

New `make saga` target runs all six saga scripts sequentially, each
independently (one skip does not block the next). Extend `make e2e` to run
`run.py` then `make saga` so the full E2E layer includes saga journeys.
Each saga script exits 0 on pass/skip, 1 on fail — `make saga` aggregates
exit codes.

## Risks / Trade-offs

- **[Flaky service health]** Probing live endpoints can flake if a service
  is slow to start. → Mitigation: reuse the existing `T` timeout constant
  from `run.py` and the `probe()` retry pattern; a service that is
  unreachable is a skip (not a fail) when its tile is not rendered.

- **[Contract drift]** The saga journeys probe endpoints that may change as
  services evolve (e.g., Invoice Ninja API paths). → Mitigation: probe
  stable public surfaces (health endpoints, auth-wall redirects) rather
  than deep API paths; document the probed endpoints in
  `docs/E2E-JOURNEYS.md` so drift is visible.

- **[CI runtime]** Six additional HTTP journeys add ~30–60 seconds to a
  full-stack E2E run. → Acceptable: they skip instantly on minimal stacks
  (no service deployed = no probe), and full-stack CI already runs the
  slower SSO flows.

- **[saga_smoke.py refactor risk]** Extracting the harness changes a file
  that CI may already reference. → Mitigation: `saga_smoke.py` keeps its
  `main()` and CLI signature; the refactor is internal (extract functions,
  same output). Run `python3 tests/05-e2e/saga_smoke.py` before and after
  to confirm identical output.
