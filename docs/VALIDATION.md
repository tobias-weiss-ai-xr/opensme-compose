# Validation

How the openSME Compose distribution is validated before release.

## Test layers

| Layer | Tooling | In CI |
|-------|---------|:---:|
| 0 · Static | yaml lint, env completeness, secret scan (`tests/00-static/`) | ✅ |
| 0 · Perf gate | `tests/00-static/check_perf.py` — budgets, limits, log caps, healthchecks | ✅ |
| 0 · Boot contracts | `tests/00-static/check_boot.py` — image pins, entrypoints, Traefik routers, healthcheck binaries | ✅ |
| 0 · Backup coverage | `tests/00-static/check_backup.py` — every stateful volume the fragments ship is in backup.sh (drift guard) | ✅ |
| 0 · Compose matrix | `tests/00-static/compose_config.py` — every overlay combination renders | ✅ |
| 1 · Specs | spec compliance (`tests/01-specs`) | ✅ |
| 2 · Contracts | service/contract validation | ✅ |
| 3 · Smoke | HTTP endpoints + container health (`tests/03-smoke`) | host |
| 4 · Integration | cross-service wiring: DB provisioning, pgbouncer, SSO issuer, live AI proxy | host |
| 5 · E2E | real SSO/OIDC journeys over HTTP (Zitadel Session API, no browser) + portal security-header contract | ✅ |
| 6 · Security | hardening audit: exposed ports, secrets, TLS, privileges | host |
| 7 · Bench | `tests/07-bench/run_bench.py` — memory + p50/p95/p99 latency | optional (manual) |
| 8 · K8s | cluster deployment health (`tests/08-k8s`) | host |
| Portal | `cargo test` in `portal/` — in-process HTTP contract tests, mock AI upstream, proptest fuzzing of parsers | ✅ |

Run everything locally: `make test-static`, `make test-all`, `make bench`,
`cd portal && cargo test`.

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
[docs/perf/baselines.md](docs/perf/baselines.md).

## Release checklist

1. `make test-static` green locally.
2. CI green on the branch (static suite, internal-ref scan, code quality
   incl. portal `cargo test`, and the E2E SSO job).
3. On a reference host: `make up` for each tier, `make test`, `make bench`
   (record results in `docs/perf/benchmark-run.md`).
4. `openspec validate` for any in-flight changes; archive when complete.
