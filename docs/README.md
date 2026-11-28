<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# openSME documentation

Index of all documentation in this repository. If you only read one thing,
read the [README](../README.md) — it is the operator manual. This folder holds
the deep dives.

> **Docs are not the source of truth.** Where prose and executable artifacts
> disagree, the executable artifact wins: compose files + [`specs/`](../specs/)
> + [`contracts/`](../contracts/) + [`tests/`](../tests/) are normative, this
> prose is explanatory. Found a drift? That is a bug — fix it in the same PR
> (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## Start here, by task

| I want to… | Read |
|---|---|
| Install the stack on my own server | [README → Quick Start](../README.md#quick-start) |
| Try it in two minutes | [README → Evaluate in 2 minutes](../README.md#evaluate-in-2-minutes) |
| Understand how the pieces fit | [ARC42.md](ARC42.md) |
| Size a host / understand the memory budget | [PERFORMANCE.md](PERFORMANCE.md), [perf/baselines.md](perf/baselines.md) |
| Update an existing deployment | [UPGRADE.md](UPGRADE.md) |
| Back up, restore, encrypt, ship off-site | [backup-restore.md](backup-restore.md), [rotate-backups.md](rotate-backups.md) |
| Make deploys byte-for-byte reproducible | [pin-images.md](pin-images.md) |
| Inventory images, SBOMs, and vulnerability/VEX evidence | [sbom.md](sbom.md) |
| Know what the tests actually prove | [VALIDATION.md](VALIDATION.md), [E2E-JOURNEYS.md](E2E-JOURNEYS.md) |
| Extend the agents without breaking governance | [AGENTIC-AI.md](AGENTIC-AI.md), [audits/](audits/) |
| Contribute code, specs or docs | [CONTRIBUTING.md](../CONTRIBUTING.md), [tests/README.md](../tests/README.md) |
| See what is planned | [ROADMAP.md](ROADMAP.md) |

## Documents

### Architecture & design

| Document | What it covers |
|---|---|
| [ARC42.md](ARC42.md) | Full arc42 architecture: goals, constraints, context, building blocks, runtime, deployment, cross-cutting concepts, decisions, quality goals, risks, glossary |
| [AGENTIC-AI.md](AGENTIC-AI.md) | How agentic capabilities are governed: saga → epic → user story → OpenSpec capability → contract → test |
| [audits/](audits/) | Audits of the agentic components (e.g. `dev-agent-watch.md`) |
| [superpowers/](superpowers/) | Design notes produced while working with the pi workflows |
| [OPENCLOUD-CAMPAIGNS-SUMMARY.md](OPENCLOUD-CAMPAIGNS-SUMMARY.md) | OpenCloud integration campaigns: results, lessons learned and remaining future work |
| [portal-migration.md](portal-migration.md) | Portal migration notes: Next.js frontend ahead of the Rust catalog |

The normative architecture is in the compose files and the contract/spec
directories:

- [`specs/`](../specs/) — expected state per service (image, ports, healthcheck, resources)
- [`contracts/`](../contracts/) — cross-cutting rules (tiles, interop, ports, networks, security, env, agents, knowledge)
- [`openspec/`](../openspec/) — living capability specs and change proposals

### Operations

| Document | What it covers |
|---|---|
| [PERFORMANCE.md](PERFORMANCE.md) | Reservations-first sizing, per-tier parameters (PostgreSQL, PgBouncer, Redis/Memcached, Traefik), boot ordering, hardening defaults, edge-first latency measurement |
| [SSO.md](SSO.md) | SSO-Architektur & Login-Durchgänge — Zitadel/OIDC-Flow, Status-Matrix aller Dienste, Login-UI, Token-Typen, atomare oidc_config (R15), Logout/Single Sign-Out (R17), Backchannel-Pin, Ops-Runbook |
| [perf/README.md](perf/README.md) | Per-tier “what may run” matrix + benchmark protocol |
| [perf/baselines.md](perf/baselines.md) | Measured resource numbers per tier |
| [perf/benchmark-run.md](perf/benchmark-run.md) | How a benchmark run is recorded (`make bench`) |
| [UPGRADE.md](UPGRADE.md) | Helm-free upgrade: pre-flight checks, modes, rolling restart, single-service upgrades, rollback |
| [backup-restore.md](backup-restore.md) | Encrypted (AES-256-CBC) backup sets, S3/rclone upload, restore incl. `--passphrase` |
| [rotate-backups.md](rotate-backups.md) | Off-site retention policy via `rclone` |
| [pin-images.md](pin-images.md) | Resolving image tags to sha256 digests → `manifests/pinned-images.env` |
| [sbom.md](sbom.md) | SBOM/VEX supply-chain gate: canonical inventory, pin allowlist, Syft/Trivy evidence, freshness and rollback |
| [mail.md](mail.md) | Mail scope: closed system by design — why no MX/SPF/DKIM/DMARC are published, what still works, and the checklist for opening it later |
| [COMPLIANCE.md](COMPLIANCE.md) | Compliance reporting (`make compliance`), report formats, scheduling |
| [ROADMAP.md](ROADMAP.md) | Current scope, in-flight work, backlog, perf-budget targets |
| [opencloud.md](opencloud.md) | OpenCloud deployment: OIDC, DAV, Office integration and the production S3 overlay |
| [opencloud-integration-matrix.md](opencloud-integration-matrix.md) | OpenCloud integration campaign matrix (tasks, results, skipped items) |
| [opencloud-testing.md](opencloud-testing.md) | OpenCloud integration test protocol and observed results |
| [operaton-sso.md](operaton-sso.md) | Operaton BPMN login via Zitadel OIDC |
| [registry-mirror.md](registry-mirror.md) | Local registry mirror / pull-through cache setup |
| [nix.md](nix.md) | Nix-built replacement images, local tags and CI publication to ghcr.io |

- [Events gateway](EVENTS-GATEWAY.md) — opt-in read-only SSE gateway runbook

| [vc.md](vc.md) | LiveKit video-meeting service, room creation and edge routing |

### Quality & verification

| Document | What it covers |
|---|---|
| [VALIDATION.md](VALIDATION.md) | Test layers, layer-0 check inventory, the static gate CI blocks on, the cron suite monitor (C1–C5), release checklist |
| [E2E-JOURNEYS.md](E2E-JOURNEYS.md) | Epics and user stories, and the exact e2e tests that prove them |
| [tests/README.md](../tests/README.md) | The pyramid layer by layer, how to run and extend it |

## Conventions for docs in this repo

1. **One job per file.** Operator procedures live in their own file, not as a
   growing section of the README. The README links; the doc explains.
2. **Relative links only.** No absolute `https://github.com/...` links to files
   in this repo — they rot on forks and branches.
3. **State the executable counterpart.** Every operational claim should name the
   script, make target, contract or test that enforces it.
4. **No environment-specific data.** No internal hostnames, private IPs or
   registry URLs — the repo is public and the static layer scans for it.
5. **Keep the index current.** Adding or renaming a doc means updating this
   file and the [README documentation section](../README.md#documentation).
6. **English**, except where a document is deliberately German (currently
   `ARC42.md`); keep new content in the language of the file it extends.
