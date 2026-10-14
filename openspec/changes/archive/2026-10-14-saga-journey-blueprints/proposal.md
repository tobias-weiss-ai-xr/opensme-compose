# Proposal: Saga Journey Blueprints

## Why

The `portal-catalog` spec requires a journey blueprint for every catalog saga
(onboarding, quote-to-cash, leave request, support ticket, publishing, team
knowledge) that "validates the participating services' public contracts and
SHALL skip cleanly when a participating service is not deployed." The
implementation delivered only `tests/05-e2e/saga_smoke.py`, which pins
**navigation edges** (portal health, tile reachability, feed degradation) for
three sagas. Five of six sagas have no journey blueprint at all, and the
existing three validate navigation, not the cross-service **public contracts**
the spec mandates. The spec is currently violated by the implementation.

## What Changes

- Implement one parametrized E2E journey module per catalog saga in
  `tests/05-e2e/`, each validating the **public contracts** of its
  participating services (not just navigation edges), and skipping cleanly
  when a participating service is not deployed.
- Refactor `saga_smoke.py` to keep the navigation-level checks (P1–P5) as a
  shared precondition harness; the new journey modules import and reuse it
  rather than duplicating portal-probe logic.
- Extend the `portal-catalog` spec with per-saga contract requirements so
  each journey has explicit acceptance criteria (which services, which
  endpoints, which skip conditions).
- Document the six journey blueprints in `docs/E2E-JOURNEYS.md` with persona,
  steps, participating services, and skip conditions (extending the existing
  saga documentation from the `sme-function-catalog` change).
- No portal, compose, or service code changes — this is a test-suite
  completeness change that closes the spec gap.

## Capabilities

### New Capabilities
<!-- none: no new capability. The catalog and its sagas already exist. -->

### Modified Capabilities
- `portal-catalog`: Add per-saga journey-blueprint requirements specifying
  which participating services' public contracts each journey validates and
  under which conditions each journey skips. The current single requirement
  ("Saga journeys stay executable") is refined into six concrete journey
  requirements.

## Impact

- `tests/05-e2e/`: new journey modules (`saga_onboarding.py`,
  `saga_quote_to_cash.py`, `saga_leave_request.py`,
  `saga_support_ticket.py`, `saga_publishing.py`,
  `saga_team_knowledge.py`); `saga_smoke.py` refactored to expose its
  precondition harness for reuse.
- `tests/05-e2e/run.py`: register the six journey modules in the E2E runner
  with skip-when-service-absent semantics (same convention as existing
  journeys).
- `docs/E2E-JOURNEYS.md`: per-saga blueprint documentation (persona, steps,
  participating services, skip conditions).
- `openspec/specs/portal-catalog/spec.md`: six new requirements (one per
  saga journey) replacing the single coarse requirement.
- No runtime code, compose, or contract changes. CI impact: the E2E layer
  gains six new journey files that skip on partial deployments, so CI on a
  minimal stack is unaffected; full-stack CI gains the contract checks.
