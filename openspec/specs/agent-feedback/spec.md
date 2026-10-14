## Purpose

Feedback loop + human-in-the-loop (HITL) for agent decisions on top of the
agent-saga write gate: a human can only decide on drafts they can see,
rejection is a first-class outcome that records the human's reason, the
reason is a queryable feedback signal, and recent feedback flows back into
the agent's LLM context — the loop closes. Major decisions are explicitly
flagged. Privacy rule from `dev-agent-privacy` holds: only anonymized bytes
leave the host.

## Requirements

### Requirement: A human can only decide on drafts they can see

The API SHALL expose `GET /drafts` listing every draft with its lifecycle
state (pending / approved / rejected), its content, and its major flag,
oldest first, so the HITL decision is made with full visibility.

#### Scenario: Listing shows the lifecycle state

- **WHEN** drafts exist in different states
- **THEN** `GET /drafts` SHALL return each draft with `approved`, `rejected`
  and `text` visible to the deciding human

### Requirement: Rejection is a first-class outcome with a reason

`POST /draft/{id}/reject` SHALL require a non-empty `reason` (400 otherwise),
SHALL record `rejected:true` plus the reason on the draft, and SHALL be
audited as kind `reject`. Rejection SHALL be final: approving a rejected
draft SHALL be refused (409) and MUST NOT commit. The raw reason SHALL stay
on the draft and MUST NOT enter the evidence log (privacy: no operator free
text in the audit trail).

#### Scenario: Rejection records the feedback

- **WHEN** a human rejects a pending draft with a reason
- **THEN** the draft SHALL be `rejected:true` with the reason recorded and a
  `reject` evidence entry SHALL exist

#### Scenario: Rejection is final

- **WHEN** a rejected draft is approved afterwards
- **THEN** the API SHALL answer 409 and the draft SHALL stay rejected and
  uncommitted

#### Scenario: Feedback without a why is refused

- **WHEN** a rejection is attempted with an empty reason
- **THEN** the API SHALL answer 400

### Requirement: Feedback is queryable

The API SHALL expose `GET /feedback` aggregating the human decisions: counts
of approved / rejected / pending drafts, the number of pending major
decisions, and every rejection with its reason, newest first.

#### Scenario: The aggregate carries decisions and reasons

- **WHEN** at least one draft was approved and one was rejected with a reason
- **THEN** `GET /feedback` SHALL report both counts and SHALL include the
  rejection reason in its `reasons` list

### Requirement: Major decisions are flagged

A draft created with `major:true` SHALL carry the major flag in its
response, in the `GET /drafts` listing, and in the draft evidence entry, so
major decisions are explicitly auditable.

#### Scenario: A major draft is auditable

- **WHEN** an agent creates a draft with `major:true`
- **THEN** the listing and the evidence entry SHALL mark it as major

### Requirement: The feedback loop closes into the agent context

The LLM prompt context SHALL include the most recent human rejections (at
most 3, newest first) rendered through the anonymizer, and the analysis
cache key SHALL include the feedback context so new feedback invalidates a
cached analysis.

#### Scenario: A rejection reason reaches the next analysis

- **WHEN** a draft was rejected with a reason and the agent runs a new LLM
  analysis
- **THEN** the prompt SHALL contain that reason (anonymized) and the analysis
  SHALL NOT be served from a cache entry created before the rejection

### Requirement: The loop is observable

`GET /metrics` SHALL expose the draft loop in Prometheus format: pending
drafts, pending major drafts, approved drafts and rejected drafts, so an
operator dashboard can watch decisions awaiting a human.

#### Scenario: The dashboard sees pending major decisions

- **WHEN** a major draft is pending
- **THEN** `dev_agent_drafts_pending` and `dev_agent_drafts_major_pending`
  SHALL both count it
