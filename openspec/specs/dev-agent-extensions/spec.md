## Purpose

Defines the pi-extension integration: `.pi/extensions/opensme-dev-agent.ts`
registers the agent with pi (capability `com.opensme.agent`) and exposes
`/status`, `/heal`, `/diag` plus the HITL commands `/drafts`, `/approve`,
`/reject` and `/feedback` that call the agent REST API, so the operator can
run the maintenance loop — including draft decisions and the feedback
signal — directly from the pi console.

## Requirements

### Requirement: Extension registers the agent with pi

The pi extension SHALL use `registerCommand` to register `/status`, `/heal`,
`/diag`, `/drafts`, `/approve`, `/reject` and `/feedback`, and SHALL
advertise the capability `com.opensme.agent` for agent discovery.

#### Scenario: Commands are registered

- **WHEN** pi loads the extension
- **THEN** `/status`, `/heal`, `/diag`, `/drafts`, `/approve`, `/reject` and
  `/feedback` SHALL be registered as commands

#### Scenario: Agent is discoverable

- **WHEN** pi queries available capabilities
- **THEN** the extension SHALL report `com.opensme.agent`

### Requirement: /status command surfaces agent state

The `/status` command SHALL call the agent `GET /status` endpoint and render
container health, last reconcile time, and any flagged symptoms.

#### Scenario: Healthy stack renders healthy

- **WHEN** `/status` runs and all watched containers are healthy
- **THEN** the command SHALL report the healthy set and last reconcile time

#### Scenario: Unhealthy container is surfaced

- **WHEN** `/status` runs and a watched container is unhealthy
- **THEN** the command SHALL name the container and its symptom

### Requirement: /heal command requires explicit confirmation

The `/heal` command SHALL call `POST /heal` but first require explicit user
confirmation, and SHALL always report the receipt (real or dry-run).

#### Scenario: Heal without confirmation is refused

- **WHEN** `/heal <container>` is invoked without confirmation
- **THEN** the command SHALL abort and print the would-be action without
  calling `POST /heal`

#### Scenario: Confirmed heal emits a receipt

- **WHEN** the user confirms the heal
- **THEN** the command SHALL call `POST /heal` and print the returned receipt

### Requirement: /diag command gathers and triages symptoms

The `/diag` command SHALL gather container symptoms via the checker, look up
diagnoses in the embedded knowledge base, and present symptom → diagnosis →
remediation, forcing the payload through the anonymizer.

#### Scenario: Diag returns knowledge-backed triage

- **WHEN** `/diag <service>` runs for a service with a runbook entry
- **THEN** the command SHALL present the matched symptom, diagnosis and
  ordered remediation steps

#### Scenario: Diag never emits private fields

- **WHEN** `/diag` produces output
- **THEN** secrets, private IPs, hostnames and user paths SHALL be absent

### Requirement: HITL commands gate the draft decisions

The extension SHALL expose the draft lifecycle: `/drafts` SHALL list drafts
with their lifecycle state and major flag (pending first), `/approve` SHALL
require explicit user confirmation before calling the approve endpoint and
SHALL never commit a rejected draft, and `/reject` SHALL require a
non-empty reason before calling the reject endpoint.

#### Scenario: Drafts listing surfaces pending major decisions

- **WHEN** `/drafts` runs while a major draft is pending
- **THEN** the listing SHALL show the draft as pending with a major marker

#### Scenario: Approve without confirmation makes no commit call

- **WHEN** the user declines the confirmation dialog of `/approve`
- **THEN** the command SHALL NOT call the approve endpoint and the draft
  SHALL stay inert

#### Scenario: Reject without a reason is refused

- **WHEN** `/reject <id>` is invoked without a reason
- **THEN** the command SHALL print usage and SHALL NOT call the reject
  endpoint (feedback needs the why)
