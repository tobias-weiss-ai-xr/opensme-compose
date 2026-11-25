# Validation

How the openSME Compose distribution is validated before release.

## Test layers

| Layer | Tooling | In CI |
|-------|---------|:---:|
| 0 · Static | yaml lint, env completeness, secret scan (`tests/00-static/`) | ✅ |
| 0 · Perf gate | `tests/00-static/check_perf.py` — budgets, limits, log caps, healthchecks | ✅ |
| 0 · Boot contracts | `tests/00-static/check_boot.py` — image pins, entrypoints, Traefik routers, healthcheck binaries | ✅ |
| 0 · BPMN compensation | `tests/00-static/check_bpmn_compensation.py` — every AI ServiceTask has a boundary event + compensation handler | ✅ |
| 0 · Backup coverage | `tests/00-static/check_backup.py` — every stateful volume the fragments ship is in backup.sh (drift guard) | ✅ |
| 0 · Compose matrix | `tests/00-static/compose_config.py` — every overlay combination renders | ✅ |
| 0 · Agent lint | `tests/00-static/check_agent.py` — dev-maintenance-bot knowledge/spec parity | ✅ |
| 0 · Suite monitor guards | `tests/00-static/check_monitoring.py` — M1–M9 pin the cron suite monitor (health/edge probes/JWT/auth-errors/**edge-latency**); local Layer-0 sweep (`tests/00-static/run.sh`), deliberately not a `run.py` CI entry | — |
| 0 · Suite coverage | `tests/00-static/check_suite_coverage.py` — no silently-dead checks (inventory below) | ✅ |
| 0 · Mutation tests | `tests/00-static/check_mutations.py` — every wired check has teeth (mutation-flipped) or a documented shadow-incompat reason; full-coverage self-check | ✅ |
| 0 · Env-quote hardening | `tests/00-static/check_env_quote.py` — brand.sh emits quoted env values; dotenv loaders scrub unquoted multi-word values (backup + warning, never die, idempotent) before `source` | ✅ |
| 0 · Agentic gate | `tests/00-static/check_agentic.py` — AS6 self-maintenance, **OPT-IN** (`AGENTIC_STRICT=1`) | — |
| 1 · Specs | spec compliance (`tests/01-specs`) | ✅ |
| 2 · Contracts | service/contract validation | ✅ |
| 3 · Smoke | HTTP endpoints + container health (`tests/03-smoke`), including the login v2 UI page at `auth.*/ui/v2/login` | host |
| 4 · Integration | cross-service wiring: DB provisioning, pgbouncer, SSO issuer, live AI proxy | host |
| 5 · E2E | real SSO/OIDC journeys over HTTP (Zitadel Session API, no browser): `run.py` (Epic A–D), `logout.py` (portal logout + single sign-out to OpenCloud), `session_lifecycle.py` (timeout, refresh rotation, cross-service SSO), `saga_smoke.py`, `webmail.py` (SOGo page contract + own database schema + a real login → account bootstrap → mailbox view → submission on 587 → delivery into the INBOX over IMAP → CalDAV/CardDAV reachability (207, own collections, Basic auth), own `mail` job), `stories.py` (epic user stories: portal anonymous/signed-in catalog contract, VC lobby→room→signaling-auth, booking→join-link interop, edge hardening, catalog-driven tile sweep, **signed-in SSO round trips against Invoice/Paperless/Operaton/Synapse via the Session API (Epic J)**, intercom/polls upload round trips), plus the portal security-header contract | ✅ |
| 6 · Security | hardening audit: exposed ports, secrets, TLS, privileges | host |
| 7 · Bench | `tests/07-bench/run_bench.py` — memory + p50/p95/p99 latency | optional (manual) |
| 8 · K8s | cluster deployment health (`tests/08-k8s`) | host |
| Live monitor | cron `*/5 * * * *` runs `scripts/suite-monitor.sh`: C1 container health, C2 edge probes (status + timing), C3 JWT mint, C4 auth-error log scan, C5 **Traefik edge** slow-request scan (mail alert + cooldown) — C5 deliberately reads the edge log, because OpenCloud's own access-log durations measure the upstream TCP connection age, not latency (see `docs/opencloud.md` §3.11) | host |
| Portal | `cargo test` in `portal/` — in-process HTTP contract tests, mock AI upstream, proptest fuzzing of parsers | ✅ |

Run everything locally: `make test-static`, `make test-all`, `make bench`,
`cd portal && cargo test`.

Layer 5 is split by job: the SSO-flows job runs `run.py` and the journeys
that share its prerequisites, while the heavier operator journeys
(broadcast, resilience, backup, restore drill, rolling recreate,
persistence, portability, intercom, …) run as their own steps in the same
job. Journeys that need an overlay the demo stack does not boot get their
own job when the overlay can be booted in CI: the `mail` job brings up
Stalwart + SOGo on top of the demo stack, registers the `sogo-webmail` OIDC
client in Zitadel (`scripts/zitadel-client.sh --app sogo`) and provisions
the demo users (`scripts/provision-demo-users.sh` with `--component
zitadel` for the IdP accounts, `--component stalwart` for the
`sogo.mail_users` rows — the Stalwart principal auto-provisions on first
login) and runs `webmail.py`, which walks the real SSO flow (Session-API
login, OIDC callback with one cookie jar) and then also sends and receives
a message as that user via XOAUTH2 (587 + STARTTLS → INBOX over IMAP 143)
and checks the calendar/contacts half over DAV (`PROPFIND` 207 with the
user's own collections — Basic auth, not the session cookie). It skips
honestly on a stack without the mail profile,
and fails when a running `opensme-sogo`/`opensme-stalwart` container stops
answering or its published ports disappear. The six `saga_*.py` blueprints
stay local (Operaton, invoice, paperless, notes, ticketing, CMS) — they run
via `make saga` or a full stack. `tests/00-static/check_agentic.py` enforces
that this split is
explicit: with `AGENTIC_STRICT=1` every `tests/05-e2e` journey must be a
CI step or on its documented allowlist.

## Layer-0 check inventory

> Machine-readable fence: `check_suite_coverage.py` verifies this block
> stays in sync with `tests/run.py` (every wired check listed here, nothing
> phantom). Edit this list when you add or rename a `tests/00-static/check_*`
> script.

<!-- CHECK-INVENTORY-BEGIN -->
- [x] `check_agent.py` — dev-maintenance-bot static lint (Layer 0)
- [x] `check_agentic.py` — AS6 self-maintenance gate (OPT-IN, not default)
- [x] `check_artifacts.py` — committed-artifact hygiene
- [x] `check_backup.py` — every stateful volume is in backup.sh (drift guard)
- [x] `check_backup_restore.py` — backup/restore contract
- [x] `check_boot.py` — image pins, entrypoints, Traefik routers, healthcheck binaries
- [x] `check_bootstrap.py` — bootstrap seed data (BPMN)
- [x] `check_bpmn_compensation.py` — every AI ServiceTask has a boundary event + compensation handler (Epic AF)
- [x] `check_env.py` — env completeness
- [x] `check_demo_env.py` — demo `.env` completeness (every `${VAR:?}` guard in the demo chain is generated by `scripts/demo.sh`)
- [x] `check_env_quote.py` — env-quote hardening: brand.sh writes double-quoted values; the byte-identical loader block in calendar-setup.sh/demo-data.sh rewrites unquoted multi-word values quoted in place (.bak-<date> + file+line warning) before `source`, never dies, idempotent
- [x] `check_interop.py` — interoperability surfaces (SSO, routing, Redis auth)
- [x] `check_mail_backend.py` — mail backend switch (XOR)
- [x] `check_monitoring.py` — suite-monitor guards M1–M9: cron monitor exists, guards (accessTokenType event-store, opaque-token signature, traefik edge-latency C5, container health, mail alerting + cooldown, cron wiring); forbids opencloud access-log durations as a latency signal (M9 — they measure the upstream TCP connection age)
- [x] `check_mutations.py` — every wired check has teeth (mutation-flipped red) or a documented shadow-incompat reason; prints `mutation-coverage: N/M` and fails on any uncovered wired check
- [x] `check_oidc.py` — Zitadel OIDC client registration
- [x] `check_outbound_patterns.py` — outbound LLM payload pattern catalog still detects secrets/IPs/paths/hosts (teeth for AE1)
- [x] `check_notes_broker.py` — notes celery broker URL uses DJANGO_ prefix + REDIS_PASSWORD (no passwordless default)
- [x] `check_webmail_sso.py` — stalwart webmail-SSO backchannel: oidc directory, idpplain userinfo hop (:8081, external Host), no hardcoded-IP pin in the shared compose (fleet overlay injects host-gateway)
- [x] `check_perf.py` — budgets, limits, log caps, healthchecks
- [x] `check_platform.py` — platform versions (k3s/docker)
- [x] `check_suite_coverage.py` — this inventory's keeper; CI static job must be single-entry (`tests/run.py --static` only — any direct `tests/00-static/*.py` call in ci.yml is a failure)
- [x] `check_bridge.py` — portal-web component bridge: typed registry contract (slots share.open/cloud.pick/task.create), every slot consumed outside the registry, shell wires share.open → notes tab + task.create → Aufgaben tab, composer posts via existing note API, feed/notes/cloud „Als Aufgabe anlegen“ forward title+link through task.create
- [x] `check_events.py` — portal real-time events: Event/Visibility/EventBus + XADD/XREAD, /api/events behind the session firewall, SSE proxy forwards cookie/last-event-id as a ReadableStream with same-origin EventSource, explicit recipient-scoped task events, polling fallback + Traefik compression exclusion
- [x] `check_cloud_bridge.py` — portal cloud-file bridge: mkdir + path-delete routes, both behind the session firewall, caller path validated, DAV client scoped to the session user, all four cloud error helpers used, catch-all proxy forwards cookie/body, browser calls both endpoints
- [x] `check_docs_index.py` — docs-as-contract: every top-level docs/*.md is linked from docs/README.md or the README documentation table, and every link in both places resolves
- [x] `check_portal_cors.py` — same-origin CORS contract: scoped edge middleware for direct DAV/OCS, no permissive CORS in the Rust portal, Next.js catch-all proxy as the single browser entry point
- [x] `check_webdav_archive.py` — opt-in WebDAV archival: invoice/Paperless sidecars are profile-gated, mount their source read-only, idle without credentials and use distinct remote folders
- [x] `check_caldav_portal.py` — CalDAV/CardDAV integration: SOGo secondary DAV sources, edge routers, env endpoint helpers and tile-based portal calendar/contacts contract
- [x] `check_tasks.py` — portal team-tasks backend contract: stores-Pfad `TASKS_DATA_PATH`/Cap `TASKS_MAX_TASKS`, Cap-Eviction nur erledigter Aufgaben, `may_complete`-Guard (nur Zuständiger/Autor bei Status/Löschen), 5 `/api/tasks`-Routen hinter `require_session`, Author aus Session, 403-Helper
- [x] `check_tiles.py` — portal function catalog
- [x] `compose_config.py` — overlay matrix
- [x] `scan_secrets.py` — secret scan
- [x] `yaml_lint.py` — YAML syntax lint
<!-- CHECK-INVENTORY-END -->

The inventory fences the full set of `tests/00-static` checks; see the
"Static gate" section for what CI actually blocks.

## Static gate (what CI blocks)

`tests/run.py --static` must pass on every commit:

- every YAML overlay parses; every secret pattern absent from the tree
  (RFC1918/link-local IPs, host paths);
- every service has `deploy.resources.limits` (cpu+memory), a `json-file`
  logging cap (50m×3), and a healthcheck when long-running;
- reservation sums per tier ≤ budgets (soho 6G / small 20G / medium 40G);
- digest-pin variables documented in `.env.example`;
- spec + contract suites green.

## Perf budgets (current)

SOHO ~0.8G, Small ~3.4G, Medium ~8.0G Σ reservations — see
[perf/baselines.md](perf/baselines.md).

## Release checklist

1. `make test-static` green locally.
2. CI green on the branch (static suite, internal-ref scan, code quality
   incl. portal `cargo test`, and the E2E SSO job).
3. On a reference host: `make up` for each tier, `make test`, `make bench`
   (record results in `docs/perf/benchmark-run.md`).
4. `openspec validate` for any in-flight changes; archive when complete.
