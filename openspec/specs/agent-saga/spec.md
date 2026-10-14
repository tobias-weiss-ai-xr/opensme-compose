## Purpose

Defines the agent write gate shared by AS2 (agentic support), AS3 (agentic
processes) and AS5 (agentic documents): an actor drafts content, the draft is
**inert** until a human approves it, and only then does it become a commit —
**every stage is audited**. A draft is never acted on on its own; discarding is
the normal case.

## Requirements

### Requirement: Drafts are inert until human approval

An agent-created draft (classification, reply, tags, …) SHALL be stored with
`approved: false` and SHALL have no side effect until a human approves it. The
draft body may hold content, but it never enters the evidence log.

#### Scenario: A fresh draft is inert

- **WHEN** an agent creates a draft via `POST /draft`
- **THEN** the response carries `approved: false`
- **AND** no commit evidence exists for it yet

#### Scenario: Creating a draft is audited

- **WHEN** a draft is created
- **THEN** the evidence log SHALL contain a `draft` entry pointing at the draft

### Requirement: Approval is a committed, audited gate

Approving a draft (`POST /draft/{id}/approve`) SHALL flip it to `approved: true`,
record a `commit` evidence entry, and be idempotent. Approving an unknown draft
SHALL return 404.

#### Scenario: Approval commits and audits

- **WHEN** a human approves an existing draft
- **THEN** the draft SHALL be `approved: true`
- **AND** the evidence log SHALL contain a `commit` entry for it

#### Scenario: Unknown draft rejected

- **WHEN** approval targets a non-existent draft id
- **THEN** the API SHALL return 404

### Requirement: Discarding an unapproved draft must leave no trace of a side effect

Because drafts start inert and the gate is approval-based, an unapproved or
discarded draft SHALL produce no side effect and MUST NOT create a `commit`
evidence entry. The safe default therefore SHALL need no special handling.

#### Scenario: An unapproved draft never commits

- **WHEN** a draft is created and left unapproved
- **THEN** no `commit` evidence entry SHALL exist for it
- **AND** the draft SHALL remain `approved: false`

### Requirement: Drafts survive a restart

Draft persistence SHALL cover the whole draft lifecycle: the state file
SHALL contain drafts with their states, major flag and rejection reasons,
startup SHALL restore them, and the id sequence SHALL continue past the
highest restored id so a fresh draft never overwrites a restored one.

#### Scenario: A pending decision outlives the bot

- **WHEN** drafts exist in different states and the bot restarts
- **THEN** `GET /drafts` and `GET /feedback` SHALL show the same drafts,
  states and reasons as before the restart

#### Scenario: The id sequence continues

- **WHEN** drafts were restored and a new draft is created
- **THEN** its id SHALL be greater than every restored id (no collision)

### Requirement: Draft TTL is opt-in queue hygiene

The bot SHALL support expiring stale pending drafts via
`DEV_AGENT_DRAFT_TTL` (duration; 0 or unset = off, the default). Expiry
SHALL only ever take the safe direction — a pending draft is marked
`expired` (final: no approve, no reject), audited (kind `expire`) and
counted separately in `/feedback` and `/metrics`; it can never commit.
Drafts persisted before the `created_at` field existed SHALL never expire
(the upgrade path must not mass-retire decisions), and `GET /drafts` SHALL
apply the policy before listing.

#### Scenario: A stale pending draft leaves the queue

- **WHEN** `DEV_AGENT_DRAFT_TTL` is set and a pending draft is older than
  the TTL
- **THEN** the next reconcile or `/drafts` listing SHALL mark it expired
- **AND** an approve on it SHALL be rejected with 409
- **AND** `dev_agent_drafts_expired_total` SHALL count it

#### Scenario: Policy off by default

- **WHEN** `DEV_AGENT_DRAFT_TTL` is unset
- **THEN** no draft SHALL ever be expired regardless of age
