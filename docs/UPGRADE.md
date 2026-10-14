# Upgrades (helm-free)

openSME is a Docker Compose distribution, so upgrades are helm-free by
design: pull the new images and re-create the containers in place. The
official tooling wraps that in a safe, rolling procedure with the same
pre-flight contract checks the CI test pyramid runs.

## The upgrade procedure

A safe upgrade is three steps, in this order:

1. **Pre-flight contract checks** — `make test-static`
   (`compose-check`, `yaml-lint`, `env-check`, `secret-scan`, spec
   compliance, contract validation). Nothing touches the live stack until
   these are green.
2. **Pull new images** — `docker compose pull` for every service.
3. **Rolling `up -d`** — one service at a time with a health check wait
   between services, so the rest of the stack keeps serving.

Run all three with a single command:

```sh
make upgrade
# or explicitly:
./scripts/upgrade.sh --all
```

`--all` is the default mode. If any step fails, the script stops and exits
`1` without moving to the next step — an upgrade is only "done" after all
three steps succeed.

## Modes

| Flag                      | What it does                                                           |
|---------------------------|------------------------------------------------------------------------|
| `--help`                  | Print usage and exit (no docker required)                              |
| `--check`                 | Pre-flight static/contract checks only; exit 0 green, 1 red           |
| `--pull`                  | `docker compose pull` (all services); exit 0 on success                |
| `--up`                    | Rolling `docker compose up -d`, health-wait between services           |
| `--all`                   | (default) `--check` → `--pull` → `--up`; exit 1 if any step fails      |
| `--service <S>`           | Upgrade only service `S` (pre-flight check + pull + rolling `up -d`)   |

## Pre-flight checks

`--check` runs the exact static layer CI enforces (`make test-static`).
It is a **hard gate**: when any check is red the script prints the
failures and exits `1` **without pulling or restarting anything**. Fix the
reported issue and re-run before upgrading.

If you only want to validate the configuration without upgrading, use
`--check` directly:

```sh
./scripts/upgrade.sh --check
```

## Rolling restart

`--up` enumerates services with `docker compose config --services` and
re-creates each one with `docker compose up -d --no-deps <service>`. After
each service it polls `docker inspect --format '{{.State.Health.Status}}'`
(up to 60s, `UP_HEALTH_TIMEOUT` to override) before moving on.

A service that never reports `healthy`, or has no healthcheck at all, only
prints a warning and the upgrade **continues** — the operator decides
whether to intervene. This deliberately avoids aborting the whole upgrade
because of one slow container.

## Single-service upgrades

To upgrade just one service (e.g. the portal) without touching anything
else:

```sh
./scripts/upgrade.sh --service portal
```

This still runs the pre-flight checks first, then pulls and rolling
`up -d`s only that service.

## Rollback advice

1. **Retain the bad change** — if the new images misbehave, you need the
   previous `docker compose` state and your data backups.
2. **Shut the stack down** (`docker compose down`) or roll back the
   offending service with `docker compose up -d --no-deps <service>`
   after re-pulling the previous image tag.
3. **Restore from backup** for any data that regressed — see
   `scripts/backup.sh` (`--volumes`), whose volume list the upgrade
   preserves. Data volumes are **not** touched by an upgrade, so a
   container-level rollback normally suffices; restore is for
   application-level data corruption.

## When to use `--check` vs `--all`

- Use **`--check`** to validate configuration/contracts without any
  side effects — CI, pre-change hygiene, debugging a red stack.
- Use **`--all`** for a normal upgrade: checks, pulls, rolling restart.
  There is no reason to hand-run `pull && up -d` anymore; the script does
  it safely with validation built in.

## Environment

- `.env` is sourced when present; a `COMPOSE_FILE` set in your shell
  always wins (same expansion as the rest of the repo). Default:
  `docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml`.
- `UP_HEALTH_TIMEOUT` (default 60s) and `UP_HEALTH_INTERVAL` (default 2s)
  tune the rolling health waits.
