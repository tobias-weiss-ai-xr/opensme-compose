## Purpose

Defines AS6 (Agentic self-maintenance): the governance gate for agents that
work on this repository (taskfleet / pi / dev-agent-extensions). A change is a
commit only when the test pyramid is green; the operator — never the agent —
decides to push or deploy. This capability is an **opt-in, non-default** gate:
it is inert (informational, never fails) unless `AGENTIC_STRICT=1` is set.

## Requirements

### Requirement: A commit must be backed by the test pyramid

Every repository change by an agent SHALL be verified by the test pyramid
(layer-0 static checks → unit tests → integration → e2e → security) before it
counts as a green commit. The gate measures this by proving that every e2e
journey in `tests/05-e2e/` is wired into CI (`ci.yml` ∪ `mailcow.yml`) unless
explicitly allowlisted.

#### Scenario: Every e2e journey is registered in CI

- **WHEN** the gate runs in strict mode
- **THEN** every e2e journey that is not allowlisted SHALL be rendered as a
  CI step

#### Scenario: Support modules are not counted as journeys

- **WHEN** a script in `tests/05-e2e/` is a support module (no runnable
  journey, e.g. `ai_mock`, `cloud_mock`, `cloud_mock_support`)
- **THEN** it SHALL be exempt from the "every journey in CI" rule

### Requirement: The gate is opt-in and inert by default

The self-maintenance gate SHALL be a non-default feature: with
`AGENTIC_STRICT` unset or `0`, it SHALL only report findings as informational
and SHALL always exit 0; only with `AGENTIC_STRICT=1` SHALL it enforce and
exit non-zero on a violation. It SHALL not be part of the regular layer‑0
suite that runs on every commit.

#### Scenario: Default is informational and green

- **WHEN** the gate runs without `AGENTIC_STRICT=1`
- **THEN** it SHALL exit 0 regardless of findings
- **AND** unregistered journeys SHALL be reported as info, not failure

#### Scenario: Strict enforces the pyramid

- **WHEN** the gate runs with `AGENTIC_STRICT=1`
- **THEN** any e2e journey missing from CI (and not allowlisted) SHALL fail
  the gate

### Requirement: No CI step may reference a phantom script

A rendered CI step that loads `tests/05-e2e/<file>.py` SHALL reference a file
that actually exists; a step pointing at a missing file is a defect the gate
catches in both modes.

#### Scenario: Phantom CI reference is a defect

- **WHEN** a CI step references a non-existent `tests/05-e2e/*.py`
- **THEN** the gate SHALL flag it (in strict mode as a failure)
