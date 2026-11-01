# bootstrap/ — Seed Data & Demo Content

This directory contains seed data and bootstrap scripts to populate openSME
services with meaningful demo content on a fresh deployment.

## bpmn/

Sample BPMN 2.0 process definitions for the Operaton workflow engine
(`--profile camunda`).

| File | Process key | Description |
|------|-------------|-------------|
| `invoice-approval.bpmn` | `opensme-invoice-approval` | Invoice approval: auto-approve ≤ 500, manager approval > 500, archive to File-Cloud on approve, notify on reject |
| `vacation-request.bpmn` | `opensme-vacation-request` | Vacation request: lead approval always, HR approval only for > 3 days, book absence + notify |

Both processes are engine-native (Operaton namespace
`http://operaton.org/schema/1.0/bpmn`) and use `operaton:expression` for
side-effect service tasks plus `operaton:historyTimeToLive` for history
cleanup.

## seed-deploy (content provisioning)

`bootstrap/seed-deploy.sh` imports `seed-content/*.md` into Paperless so a
fresh stack ships with real documents, not just identity rows. It drops the
files into the container's internal consumption directory (paperless only
consumes supported extensions, so the seed markdown lands as `.txt` — the
title stays the same) and confirms the import server-side via `manage.py`;
no API token is created or exposed. Idempotent by exact title.

```bash
bash bootstrap/seed-deploy.sh         # import/new-stack provisioning
bash bootstrap/seed-deploy.sh --delete # remove exactly the seeded documents
make seed                              # same as the first command
```

It is also provisioned automatically via
`scripts/provision.sh --components content` (skipped when the paperless
profile is not running) and therefore part of `./scripts/bootstrap.sh`.

### Deploying

```bash
# Engine running locally (default: auto-detects the opensme-camunda
# container and deploys via docker exec — port 8080 is not published):
./bootstrap/bpmn-deploy.sh

# Engine reachable over HTTP(S) (e.g. behind Traefik):
BPM_URL=https://bpm.example.org ./bootstrap/bpmn-deploy.sh

# or via make:
make bpm-deploy
```

The script waits for the engine to be ready, then deploys every `.bpmn`
file via `POST /engine-rest/deployment/create`. Re-running updates
existing deployments (idempotent).

After deployment, the processes are available in:
- **Cockpit**: `https://bpm.<domain>/operaton/app/cockpit/` (SSO)
- **Tasklist**: `https://bpm.<domain>/operaton/app/tasklist/` (SSO)
- **REST API**: internal-only / docker exec — see below (public `/engine-rest` is blocked).

> [!IMPORTANT]
> Since 2026-10-31 the public `/engine-rest` is BLOCKED by a Traefik edge
> router (`noop@internal`, priority 10000; on the compose-internal Traefik
> the same via the `camunda-rest-block` label router). Operaton's run dist
> only SSO-protects the webapps (Cockpit/Tasklist/Admin), the REST API stays
> unauthenticated — anonymous Internet access meant anyone could list/
> start/delete/deploy BPM processes (proven live: anonymous instance start).
> First-party tooling above runs via docker exec into the engine (loopback)
> and is unaffected. All REST automation happens **inside the compose
> network / docker exec**, never over the public host.

### Starting a process instance (internal / docker exec)

```bash
# Inside the engine container (loopback, no Internet exposure):
docker exec opensme-camunda curl -s -X POST \
  "http://localhost:8080/engine-rest/process-definition/key/opensme-invoice-approval/start" \
  -H "Content-Type: application/json" \
  -d '{"variables": {"amount": {"value": 750, "type": "Integer"}, "manager": {"value": "demo", "type": "String"}}, "businessKey": "INV-2026-001"}'

# Vacation request — two-level path (> 3 days):
docker exec opensme-camunda curl -s -X POST \
  "http://localhost:8080/engine-rest/process-definition/key/opensme-vacation-request/start" \
  -H "Content-Type: application/json" \
  -d '{"variables": {"days": {"value": 10, "type": "Integer"}, "teamLead": {"value": "demo", "type": "String"}, "substitute": {"value": "jane", "type": "String"}}, "businessKey": "VAC-2026-001"}'

# From a container on the same compose network: http://camunda:8080/...
```

### Completing a user task

```bash
# List open tasks for a user
curl "https://bpm.<domain>/engine-rest/task?assignee=demo"

# Complete the first one (approve):
curl -X POST "https://bpm.<domain>/engine-rest/task/<task-id>/complete" \
  -H "Content-Type: application/json" \
  -d '{"variables": {"approved": {"value": true, "type": "Boolean"}}}'
```

### Process variables

| Process | Variable | Type | Purpose |
|---------|----------|------|---------|
| `opensme-invoice-approval` | `amount` | Integer | Routing threshold (> 500 → manager) |
| | `manager` | String | Assignee of the manager approval task |
| | `approved` | Boolean | Set by the manager task |
| `opensme-vacation-request` | `days` | Integer | Routing threshold (> 3 → HR approval) |
| | `teamLead` | String | Assignee of the lead approval task |
| | `substitute` | String | Requested stand-in (informational) |
| | `leadApproved` | Boolean | Set by the team lead (long path) |
| | `approved` | Boolean | Set by the lead (short path) |
| | `hrApproved` | Boolean | Set by HR (long path) |

## demo user provisioning

`../scripts/provision-demo-users.sh` seeds the same 12-person SME company
(`demo-users.json`) into every component with its own user store:

| Component | Store | Mechanism |
|-----------|-------|-----------|
| Zitadel | IdP | machine-PAT admin API (default, `--component zitadel`) |
| Operaton | BPM engine identity | engine-rest via docker-exec curl (`--component operaton`) |
| Synapse | Matrix homeserver | shared-secret register API, MAC computed inside the container (`--component synapse`) |
| Paperless | document vault | Django `manage.py shell` (`--component paperless`) |
| Stalwart | mail | webadmin REST API (`--component stalwart`) |
| Invoice Ninja | billing | intentionally unwired — users are company-bound; the demo ships a single admin |

```bash
./scripts/provision-demo-users.sh --component operaton,synapse   # subsets
./scripts/provision-demo-users.sh --component all                # everything, best effort
```

All components are idempotent — re-runs report `exists` instead of
duplicating. Container targets auto-detect via `opensme-*` container
names; override with `OPERATON_URL`, `SYNAPSE_CONTAINER`,
`PAPERLESS_CONTAINER`, `STALWART_CONTAINER`, `STALWART_MAIL_DOMAIN`.

## Adding more seed data

- **BPMN processes** — drop a `.bpmn` file in `bpmn/` and re-run
  `bpmn-deploy.sh`. Keep the process `id` distinct from the
  `<definitions>` `id` and set `operaton:historyTimeToLive` on the
  `<process>` element, or Operaton rejects the deployment.
- **Portal banners** — set `PORTAL_ANNOUNCEMENTS` in `.env` to a JSON
  array of `{"level":"info|warn","text":"…"}` objects.
- **Example survey** — `make survey-deploy` (or `./bootstrap/survey-deploy.sh`)
  provisions the seeded survey „Beispiel: Datei-Upload“ (text question +
  file-upload question, active, public). Uploaded files are mirrored to
  OpenCloud by the `limesurvey-cloud-sync` sidecar (folder
  „Umfragen/LimeSurvey“). Idempotent — re-running prints the URL only.
  Custom seed surveys can later live in `surveys/` as `.lss` files
  (import via the LimeSurvey admin UI).

## seed-content/

Human-readable sample documents for your document store (Cloud /
Paperless) and as reference for operators. Not boot-consumed and never run
in CI — see `seed-content/README.md` for how to load each file.
