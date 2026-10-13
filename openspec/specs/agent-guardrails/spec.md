## Purpose

Defines the guardrails contract for every agent in the stack
(docs/AGENTIC-AI.md §5): explicit tool allowlists and write modes, a
per-run outbound budget that an agent must self-enforce, consent gates that
default to off, and the machine-checkable `contracts/agents.yml` that ties
each agent's declared capabilities to its actual compose wiring
(`tests/00-static/check_agent.py`, Layer 0).

## Requirements

### Requirement: Every agent is declared in the agents contract

The stack SHALL maintain `contracts/agents.yml` and Layer-0 SHALL validate
that each declared agent has a matching compose service with wired env flags
(consent/kill-switch/budget) and no published host ports.

#### Scenario: Contract is complete

- **WHEN** `tests/00-static/check_agent.py` runs
- **THEN** every agent in `contracts/agents.yml` SHALL resolve to an existing
  compose service and a non-empty tool list

#### Scenario: Contract matches compose wiring

- **WHEN** an agent declares a `consent_env`, a `kill_switch_env` or a
  `budgets.max_requests_env`
- **THEN** the compose service environment SHALL reference that variable
  (defaulting to the safe value) or the Layer-0 check SHALL fail

### Requirement: Outbound LLM budget is self-enforced per run

The dev-maintenance-bot SHALL cap outbound LLM calls per run via
`DEV_AGENT_LLM_MAX_REQUESTS` (0 = unlimited, wired by default). Once the
budget is exhausted, further analysis SHALL return `ErrBudgetExceeded`
without dialing, `Reconcile` SHALL pause the cycle, and the truncation SHALL
be recorded in the evidence log (kind `budget`) and in metrics.

#### Scenario: Budget blocks excess calls

- **WHEN** the budget is exhausted and a new distinct finding needs analysis
- **THEN** no outbound request is made
- **AND** the caller receives `ErrBudgetExceeded`

#### Scenario: Cache hits do not consume budget

- **WHEN** the same finding context is analyzed again within the cache TTL
- **THEN** the cached result is returned without an outbound request
  and without spending budget

#### Scenario: Exhaustion pauses the reconcile cycle

- **WHEN** `Reconcile` hits `ErrBudgetExceeded`
- **THEN** it SHALL record one evidence entry of kind `budget`, increment
  `dev_agent_llm_budget_truncations_total` and stop analysing further
  findings until the next run

#### Scenario: Unlimited is the conservative default

- **WHEN** `DEV_AGENT_LLM_MAX_REQUESTS` is unset
- **THEN** the bot SHALL run with 0 = unlimited (today's behavior), keeping
  the cap opt-in via `.env`/compose

### Requirement: Consent gates default to off

Write-capable agents SHALL expose an explicit opt-in env flag that defaults
to `false`; without it the agent SHALL only produce dry-run receipts.

#### Scenario: Heal without consent is a dry run

- **WHEN** `DEV_AGENT_ALLOW_HEAL` is unset or false
- **THEN** heal actions SHALL not execute and SHALL emit dry-run receipts

### Requirement: Kill switches stop agents without redeploy

Each long-running agent SHALL have an env kill switch (feature flag or
consumer-side switch) whose false/unset state SHALL stop the agent's
destructive or outbound behavior.

#### Scenario: Disabled predictive agent does not act

- **WHEN** `PREDICTION_ENABLED=false`
- **THEN** the predictive agent SHALL log disabled and produce no advice

#### Scenario: Assistant is invisible when unconfigured

- **WHEN** `AI_API_URL` is empty/unset
- **THEN** the portal SHALL hide the AI card and route (Epic D degrade)
