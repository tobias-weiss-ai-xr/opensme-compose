## Purpose

Defines the retrieval contract for the embedded knowledge base
(`opensme-knowledge/*.json`, dev-agent-knowledge spec): every runbook file is
**versioned** (aktualität), every answer the agent produces carries its
**source** (Quellenpflicht / source duty), and the knowledge base stays
**PII-free**. It is enforced in two places at once — the Go loader refuses to
start on a violation, and the Layer 0 static check fails CI on drift.

## Requirements

### Requirement: Every runbook file carries a version

Each `*.json` in `opensme-knowledge/` SHALL declare `kb_version`; a file
without it is rejected at load time and by the static check. The version
travels on the answer evidence so consumers can tell how current it is.

#### Scenario: Versionless runbook is rejected

- **WHEN** a runbook file omits `kb_version`
- **THEN** the Go loader SHALL fail to start
- **AND** the Layer 0 static check SHALL report a missing `kb_version`

#### Scenario: File version surfaces on answer evidence

- **WHEN** the dev-agent matches a symptom to a runbook
- **THEN** the returned match SHALL carry the source file name and the
  file's `kb_version`

### Requirement: Answers carry their source (source duty)

Every knowledge `Match` SHALL expose `source` (the file it came from) and
`kb_version`, and the status/answer rendering SHALL display them, so an
operator can trace any advice back to its runbook.

#### Scenario: Status shows provenance

- **WHEN** status is printed with matching runbooks
- **THEN** each entry SHALL show the service, version and source file

#### Scenario: /status includes source on matches

- **WHEN** `/status` returns matches
- **THEN** each match SHALL include `source` and `kb_version`

### Requirement: Knowledge base is PII-free

No runbook SHALL embed secrets, credential URIs, JWTs, user home paths or
private-address literals. The static check SHALL scan every file and fail CI
on a leak, mirroring the dev-agent's anonymizer strip set.

#### Scenario: A leaked secret fails the static check

- **WHEN** a runbook file contains a real credential value
- **THEN** the static check SHALL report it as a PII leak

#### Scenario: Prose and placeholders stay quiet

- **WHEN** a runbook only describes a state (e.g. "password invalid") or
  uses a placeholder (e.g. `Bearer <pat>`)
- **THEN** the static check SHALL NOT flag it
