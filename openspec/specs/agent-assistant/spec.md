## Purpose

Defines the portal AI assistant contract (Epic O, `portal-assistant` in
`contracts/agents.yml`): when AI is unconfigured the assistant degrades to
invisible (Epic D), and when it is enabled it never forwards user data into
the prompt without explicit opt-in. The assistant speaks a provable upstream
contract to the LLM backend (bearer auth, pinned model, bounded tokens).

## Requirements

### Requirement: Assistant degrades to invisible when unconfigured

When `AI_API_URL` is empty/unset the portal SHALL hide the AI surface and
leave it hidden through the whole navigation; no AI card, no dead route.

#### Scenario: Hidden without AI_API_URL

- **WHEN** the stack runs with no `AI_API_URL`
- **THEN** the landing page SHALL render no AI card
- **AND** requests to the AI endpoint SHALL be rejected (never forwarded)

#### Scenario: Restored to hidden after a journey

- **WHEN** an AI journey tears down its configuration
- **THEN** the AI card SHALL disappear again and the page SHALL still answer

### Requirement: No user data in the prompt without opt-in

The assistant SHALL send the user's question to the backend only after an
explicit opt-in, and SHALL bound the request (bearer auth, model, bounded
`max_tokens`). An empty question SHALL be rejected without reaching the
backend.

#### Scenario: Mock round trip proves the contract

- **WHEN** a chat request is posted through the portal proxy
- **THEN** the upstream request SHALL carry bearer auth, the configured
  model, the user message, and a bounded `max_tokens`

#### Scenario: Empty question never forwarded

- **WHEN** a blank question is posted
- **THEN** the portal SHALL return 400 and the backend SHALL receive nothing

### Requirement: Consent is explicit and reversible

Enabling AI is an operator opt-in via environment (`AI_API_URL`); it must be
possible to switch it off without redeploying the whole suite, and the
observed stack behaviour SHALL match the configured state.

#### Scenario: Operator opt-in via env

- **WHEN** `AI_API_URL` is set (and a matching model/key)
- **THEN** the AI card SHALL appear and the chat SHALL answer

#### Scenario: Reversible without code change

- **WHEN** `AI_API_URL` is removed
- **THEN** the assistant SHALL degrade back to invisible
