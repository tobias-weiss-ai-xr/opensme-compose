# Audit: dev-maintenance-bot watch/reconcile subsystem (DA-1)

Date: 2026 · Scope: `monitoring/nix/dev-agent-files/dev_agent.py`, `monitoring/dev-agent.yml`,
`opensme-dev-agent/Dockerfile`, `monitoring/nix/dev-agent-files/{Dockerfile,entrypoint.sh,healthcheck.sh}`,
`.pi/extensions/opensme-dev-agent.ts`. Method: static read-and-fix. No docker stack started.

## Findings

| # | SEV | Area | Location | What is wrong | Fix |
|---|-----|------|----------|---------------|-----|
| F1 | HIGH | watch | `monitoring/dev-agent.yml:59` | Python agent filters by **container-name substring**, but the overlay defaulted `OPERATOR_WATCH_NAMESPACES` to `opensme-compose` (copied from the Go bot's exact-match project-label default). The stack's containers are explicitly named `opensme-*` (opensme-traefik, …), so the substring `opensme-compose` matched **zero** containers — the agent silently watched nothing by default. The `.env.example` already documents the correct `opensme,default`. | Applied: default → `opensme,default` (+ comment explaining the two different filter models). |
| F2 | HIGH | pi extension | `.pi/extensions/opensme-dev-agent.ts:20-21` | `STATE_VOL = "opensme_dev-maintenance-bot-state"` does not name the volume the stack actually creates. Compose prefixes volumes with the project name (verified `opensme-compose` on a running container; `scripts/backup.sh:163` default). `docker run -v` then **auto-creates** a different, empty volume, so `/status` and `/diag` read nothing persisted. Tests/03-smoke has the same stale name (out of scope). | Applied: default → `opensme-compose_dev-maintenance-bot-state`, overridable via `DEV_AGENT_STATE_VOL`. |
| F3 | MED | reconcile | `dev_agent.py:493` | Startup warmup always dialed `call_ollama` no matter what `LLM_BACKEND` is set to. For saia/tud/openai deployments it warmed up (and could stall up to `OLLAMA_TIMEOUT`=180s) on the wrong endpoint before `startup_complete` — while Docker's start-period is only 60s → flapping "unhealthy". | Applied: warmup now dispatches through `call_llm("Hello")` (respects backend). |
| F4 | MED | watch | `dev_agent.py:72` | `UNHEALTHY_HEALTH` included `"starting"` — a transient Docker health state during the healthcheck start-period. Every normally-starting container was flagged unhealthy and sent to the LLM (false positives, wasted calls/noise). The module docstring's own contract is "exited, restarting, unhealthy, OOMKilled". A never-healthy container transitions to `unhealthy` anyway after start-period. | Applied: removed `"starting"` from the set (+ comment). |
| F5 | MED | HTTP | `dev_agent.py:559` | Single-threaded `HTTPServer` with a blocking `docker_available()` (up to 5s `run_cmd`) in the `/status` request path — one slow request stalls `/healthz` and `/ready` past the compose healthcheck timeout (5s). | Applied: `ThreadingHTTPServer` (stdlib). |
| F6 | MED | cache | `dev_agent.py:46,382` | `analysis_history` is bounded (`HISTORY_MAX` deque, persisted) but `analysis_cache` is unbounded: stale entries are only dropped on lookup, so never-revisited keys (e.g. state-churn names) accumulate on a long-running agent; `/cache` and `/metrics` dump them all. | Applied: cap `CACHE_MAX_ENTRIES` (default 500), prune expired entries past the cap. |
| F7 | LOW | version drift | `dev_agent.py:31` vs `monitoring/dev-agent.yml:53` | `OPERATOR_VERSION` was hardcoded `"3.1.0-docker"` while compose/entrypoint pass/echo `"3.1.0"` — `/status` and the startup banner disagree with the entrypoint banner. | Applied: read from env, fallback `"3.1.0-docker"` (entrypoint default unchanged). |
| F8 | LOW | config drift | `monitoring/dev-agent.yml:84`, Dockerfiles EXPOSE 8081 | `OPERATOR_HEALTH_PROBE_BIND_ADDRESS: 0.0.0.0:8081` is dead: the agent consolidated all endpoints on the `OPERATOR_METRICS_BIND_ADDRESS` port (8080) and never reads the 8081 var. Harmless, cosmetic surface. | Deferred (cosmetic; no behavior impact). |

## Recommended follow-ups (deferred by design)

1. **Smoke test volume name** — `tests/03-smoke/check_dev_agent.sh:12` still hardcodes
   `opensme_dev-maintenance-bot-state`; it passes only because `docker run -v` auto-creates a
   throwaway volume. Align it with F2 (and ideally resolve the volume via `docker compose config`).
2. **Warmup timeout vs start-period** — the warmup still uses `OLLAMA_TIMEOUT` (180s) while the
   compose healthcheck start-period is 60s; consider a dedicated short warmup timeout or async warmup.
3. **PYTHON agent has no secret/token anonymizer** — logs sent to the LLM may contain secrets;
   the Go bot has an anonymizer (`DEV_AGENT_HOSTNAMES`), the python observer has none. Evaluate
   scrubbing before `build_analysis_prompt`.
4. **Type the state-volume lookup** — the extension could discover the real volume instead of
   hardcoding (e.g. `docker compose config --format json`), eliminating F2-class regressions under
   custom `COMPOSE_PROJECT_NAME`.
5. **Expose `CACHE_MAX_ENTRIES` in `monitoring/dev-agent.yml`** if operators want to tune it.

## Verified clean

- **Loop resilience**: `reconcile()` is wrapped in try/except; `run_cmd` returns `(1,"",exc)` for any
  failure incl. `TimeoutExpired`; `call_*`/`analyze_container` swallow their own errors — one bad
  container or LLM call cannot abort the loop.
- **Timeouts bounded**: all `run_cmd` calls bounded (5/10/30s) and all LLM calls bounded
  (`OLLAMA_TIMEOUT`); no unbounded network/subprocess waits remain.
- **History persistence + bounds**: `load_history()` restores the volume file, drops malformed data
  silently, deque is `maxlen=HISTORY_MAX`, file rewritten each reconcile — survives restarts, bounded.
- **Secret hygiene**: API keys are env-only, never logged; HTTP Authorization header never printed
  (urllib errors carry no headers); prompts carry only container metadata + logs; no `eval`/`exec`/
  `pickle` of model output (output parsed with `json.loads` only).
- **Path/env consistency**: `WORKDIR /opt/dev-agent` + `COPY dev_agent.py entrypoint.sh healthcheck.sh`
  match `entrypoint.sh`'s `exec python3 /opt/dev-agent/dev_agent.py`; runtime dirs (`/var/lib/opensme`,
  `/var/log/opensme`, `/var/cache/opensme`, `/run/opensme`) are created by entrypoint and matched by
  `HISTORY_FILE`, the `dev-agent-state:/var/lib/opensme` mount and the Dockerfile `VOLUME`.
- **healthcheck wiring**: Dockerfile `healthcheck.sh liveness` and compose `curl :8080/healthz` both
  probe the single server (port parsed from `OPERATOR_METRICS_BIND_ADDRESS`, `##*:`); python:3.13-slim
  ships curl. Python 3.13 vs stdlib-only code: compatible.
- **Go bot vs python observer separation**: `dev-maintenance-bot` (Go, one-shot, exact project-label
  match, `DEV_AGENT_WATCH=opensme-compose` **correct**) vs `dev-agent` (python, name-substring match,
  now defaulting to `opensme` **correct**) — `DEV_AGENT_LLM_BACKEND` opt-in isolation intact.

## Verification

Acceptance gate passed: `test -s docs/audits/dev-agent-watch.md` ✓ ·
`python3 -m py_compile dev_agent.py` ✓ · `bash -n entrypoint.sh` ✓ · `bash -n healthcheck.sh` ✓ ·
`monitoring/dev-agent.yml` YAML-valid ✓.
