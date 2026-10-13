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
  local AI via llama.cpp (`--profile ai`) with a portal AI assistant.
- **Monitoring agents**: dev-agent (reactive, LLM root-cause),
  dev-maintenance-bot (consent-gated healing with embedded runbook KB),
  predictive-agent (Kalman/Markov risk scoring), taskfleet orchestration.
- **SeaweedFS** replaces the dead MinIO community edition behind the same
  S3 API (`--profile`-less production overlay).

## In flight

- Nothing in flight — contributions welcome (see Backlog).

## Backlog / ideas

- **Agentic AI integration** — blueprint with sagas AS1–AS6, epics AD–AH,
  specs and `contracts/agents.yml`: see `docs/AGENTIC-AI.md`.
- Digest-pinned image manifests for reproducible deploys (operator opt-in).
- Backup rotation to off-site object storage (rclone).
- Helm-free upgrades tooling: `docker compose pull` + rolling `up -d` with
  pre-flight contract checks.
- Cluster mode (out of scope by design — SME single-node focus).
- Permanent live benchmark evidence on a reference host per tier
  (`make bench`, recorded in `docs/perf/benchmark-run.md`).

## Perf budgets (target, enforced in CI)

| Tier | Σ reservations | Budget |
|------|---------------|--------|
| soho  | ~0.8G | 6G  |
| small | ~3.4G | 20G |
| medium| ~8.0G | 40G |

Numbers: [docs/perf/baselines.md](docs/perf/baselines.md).
