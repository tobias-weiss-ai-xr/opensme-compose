# Roadmap

Status and direction of the openSME Compose distribution.

## Current scope (done)

- **MVP runs on Docker Compose v2** on public images (Zitadel SSO,
  OpenCloud, SOGo+Stalwart, Traefik, PostgreSQL, Redis) plus SME staples:
  Invoice Ninja, Paperless-ngx, CryptPad, Matrix chat, Notes.
- **3 vertical tiers** (soho / small / medium) + demo profiles; overlay
  system with `COMPOSE_FILE` composition.
- **Layered test pyramid** (static → e2e → security) wired into CI,
  including secret scanning, boot contracts, a compose-matrix gate and a
  **perf-efficiency gate** (`tests/00-static/check_perf.py`); the portal
  ships its own unit suite (HTTP contract tests + proptest fuzzing).
- **Performance pass**: universal log caps, hardening defaults (`init`,
  `no-new-privileges`, `cap_drop`), per-tier Postgres/Redis/PgBouncer
  tuning, boot-ordering `depends_on`, live benchmark harness.
- **Optional business services**: Nosdesk ticketing (`--profile ticketing`),
  Operaton BPMN workflow (`--profile camunda`),
  crap-cms website (`--profile cms`), RaisFast store (`--profile store`),
  LimeSurvey surveys (`--profile surveys`), local AI via llama.cpp
  (`--profile ai`) with a portal AI assistant.
- **Seed data** for fresh deployments: BPMN processes (`make bpm-deploy`),
  human-readable seed documents, an example survey (`make survey-deploy`)
  and 12 demo users (`make provision-demo-users`).
- **Browser login page at the IdP**: the Zitadel Login v2 UI
  (`zitadel-login`, `ghcr.io/zitadel/zitadel-login`) is deployed and
  path-routed at `https://auth.<domain>/ui/v2/login`, so humans can click
  through OIDC logins; `authorize` hands off to it and the smoke layer
  probes the page. Authentication to the Zitadel API rides the seeded
  `IAM_LOGIN_CLIENT` service user's PAT (shared machinekey volume) — no
  extra provisioning step.
- **Monitoring agents**: dev-agent (reactive, LLM root-cause),
  dev-maintenance-bot (consent-gated healing with embedded runbook KB),
  predictive-agent (Kalman/Markov risk scoring), taskfleet orchestration.
- **SeaweedFS** replaces the dead MinIO community edition behind the same
  S3 API (`--profile`-less production overlay).
- **Operator tooling**: digest-pinned image manifests (`scripts/pin-images.sh`),
  off-site backup rotation via rclone (`scripts/rotate-backups.sh`) and
  helm-free upgrades with pre-flight checks (`scripts/upgrade.sh`).
- **Agentic governance**: sagas, epics, specs and contracts for the agent
  components — see `docs/AGENTIC-AI.md`; `contracts/agents.yml` is enforced
  by Layer-0.

## In flight

- Nothing in flight — contributions welcome (see Backlog).

## Backlog / ideas

- **Agentic AI integration** — remaining sagas/epics (AS1–AS6, AD–AH); the
  blueprint lives in `docs/AGENTIC-AI.md`, the opt-in self-maintenance gate is
  `make agentic-gate` (`AGENTIC_STRICT=1`).
- **Cluster mode** (out of scope by design — SME single-node focus).
- **Permanent live benchmark evidence** on a reference host per tier
  (`make bench`, recorded in `docs/perf/benchmark-run.md`).
- **Docs-as-contract**: keep `docs/README.md` and the README documentation
  table in sync with every new doc (review checklist item).

## Perf budgets (target, enforced in CI)

| Tier | Σ reservations | Budget |
|------|---------------|--------|
| soho  | ~0.9G | 6G  |
| small | ~3.5G | 20G |
| medium| ~8.2G | 40G |

Numbers: [perf/baselines.md](perf/baselines.md).
