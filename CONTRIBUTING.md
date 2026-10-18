<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# Contributing to openSME

openSME is an **open-source project, not a product**. Contributions are
welcome: bug reports, service overlays, runbooks, documentation fixes, test
teeth, and cleanups that delete more than they add.

This file covers the mechanics. The [README](README.md) explains what the stack
is; [docs/README.md](docs/README.md) is the documentation index.

## Ground rules

1. **The contract pyramid is normative.** `specs/` + `contracts/` + `tests/`
   define what the stack must do; prose explains it. A behaviour change lands
   with the spec/contract/test that pins it, or it is not done.
2. **CI must be green.** `make test` locally, plus `make lint-code`. CI runs
   the same gates — see [VALIDATION.md](docs/VALIDATION.md).
3. **This repository is public.** No secrets, no private IPs, no internal
   hostnames, no internal registry URLs, no committed backups. The static layer
   and the `leak-scan` CI job fail the build on violations.
4. **One concern per change.** A service bump, a doc rewrite and a compose
   refactor are three PRs, not one.
5. **No paid/private dependencies.** Public images from public registries,
   Apache-2.0-compatible licences only.

## Development setup

Requirements: Linux, Docker 24+ with Compose v2.20+, Python 3.11+, Rust (portal),
Go 1.22+ (dev-maintenance-bot), `shellcheck`.

```bash
git clone https://github.com/tobias-weiss-ai-xr/openSME-compose.git
cd openSME-compose
make bootstrap              # generates .env, installs test dependencies (pyyaml)
make test-static            # layers 0–2, no containers needed — start here
```

Per-component loops:

```bash
# Portal (Rust/Axum) — http://localhost:8080
cd portal && cargo run

# dev-maintenance-bot (Go): API, checker, healer, runbook KB
go test ./... && go vet ./...

# Test pyramid (see tests/README.md for layer-by-layer detail)
python3 tests/run.py --static         # layers 0–2
python3 tests/run.py                  # layers 0–4
python3 tests/run.py --help           # every flag
```

A full boot for e2e work — no configuration needed, random passwords, health
summary printed:

```bash
./scripts/demo.sh                     # localhost:8080
python3 tests/05-e2e/run.py --insecure opensme.local
```

## The workflow for a change

1. **Open a change proposal** (OpenSpec) for anything that adds or changes
   behaviour: `openspec/changes/<date>-<slug>/` with `proposal.md`, `specs/`
   deltas, `design.md` when a trade-off is involved, `tasks.md` for the work
   breakdown. Small fixes (typos, doc links) skip this.
2. **Write the failing check first.** Extend the layer whose subject you are
   touching — static (invariants), specs (expected state), contracts
   (cross-cutting rules), integration, e2e, security. Prove the check is red.
3. **Implement until green**, keeping resource limits, healthchecks, log caps
   and SSO wiring intact (the perf/security gates enforce them).
4. **Update docs in the same PR.** If you add a service: the overlay table in
   the README, `.env.example`, the tier budgets if it consumes memory, and
   `docs/README.md` if you added a doc.
5. **Archive the change** once merged and verified.

Full artifact guidance: `.pi/skills/openspec-*` and `openspec/config.yaml`.

## Adding a service overlay

Checklist, in the order CI will complain about it:

- [ ] `services/<name>.yml` with the shared `x-service-defaults`, `profiles:`,
      `deploy.resources.limits` **and** `reservations`, `logging` caps,
      `healthcheck`, `networks: [opensme-net]`, `security_opt: no-new-privileges`.
- [ ] Traefik labels: `Host()` rule, `tls=true`, `certresolver=letsencrypt`,
      `entrypoints=websecure`, explicit load-balancer port.
- [ ] No published host ports (except deliberate, documented SMTP/IMAP cases).
- [ ] `.env.example` entries for every new variable (`CHANGEME_*` for secrets)
      and a row in the README configuration table.
- [ ] `specs/services.yml` entry (image, ports, healthcheck, limits, env).
- [ ] Contract rules where applicable (`contracts/*.yml`) and the matching
      static check (`tests/00-static/`).
- [ ] SSO via Zitadel unless explicitly exempted — note the exemption in the
      spec.
- [ ] README: overlay table row, “What’s inside?” row, maturity notes if the
      upstream is alpha/BSL.
- [ ] Backup coverage: stateful volumes added to `scripts/backup.sh`
      (`check_backup.py` enforces this).
- [ ] A test with teeth — if you add a static check, add a mutation for it in
      `tests/00-static/check_mutations.py`.

## Commit & PR conventions

- Conventional commits, scoped by area or task id:
  `feat(T-OBS): …`, `fix(portal): …`, `docs(readme): …`, `test(layer0): …`.
- Keep the subject imperative and under ~72 characters; explain *why* in the
  body when the change is not obvious.
- Reference the OpenSpec change id and the failing check in the PR description.
- PR description: what changed, what proves it (command + output), what is
  deliberately out of scope.
- Never force-push a branch under review; add commits instead.
- Do not commit `.env`, `backups/`, `manifests/pinned-images.env`,
  `mail/mailcow.conf`, `idm/secrets/*`, or `portal/target/`.

## Writing documentation

The conventions live in [docs/README.md](docs/README.md#conventions-for-docs-in-this-repo).
Short version:

- One job per file; the README links, the doc explains.
- Relative links only; name the script, make target or test that enforces each
  operational claim.
- No environment-specific data.
- Adding or renaming a doc updates [docs/README.md](docs/README.md) **and** the
  [README documentation table](README.md#documentation).

## Reporting bugs

Include: the command you ran, the output, the profile/overlay combination
(`PROFILE=…`, `COMPOSE_FILE=…`, `MAIL_BACKEND=…`), the image pins in play
(`docker compose config --images`), and the relevant `docker compose logs`
excerpt. Redact hostnames and secrets before pasting.

## Security issues

Do not open a public issue for a vulnerability — email
[opensme@graphwiz.ai](mailto:opensme@graphwiz.ai) with a description and a
reproduction. See the production checklist in [README → Security](README.md#security).

## Licence

Contributions are accepted under the [Apache License 2.0](LICENSE.md). By
opening a pull request you confirm you have the right to submit the code under
that licence.
