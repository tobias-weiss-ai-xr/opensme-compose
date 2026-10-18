# Test Harness

Spec → Contract → Test scaffold for openSME Compose.

## Structure

```
tests/
├── conftest.py                  # Shared utilities (compose loader, spec loader, etc.)
├── run.py                       # Main entry point — runs all layers
├── requirements.txt             # Python dependencies (pyyaml)
├── 00-static/                   # Layer 0: Static validation (no containers)
│   ├── check_env.py             #   Env var completeness (.env.example)
│   ├── scan_secrets.py          #   Secret scanning (no CHANGEME_ in compose)
│   ├── check_artifacts.py       #   No committed backups/dumps/secrets
│   ├── check_platform.py        #   Runtime platform min versions (k3s, docker)
│   ├── check_perf.py            #   Perf gate: budgets, limits, log caps, healthchecks
│   ├── check_boot.py            #   Boot contracts (image pins, entrypoints, Traefik…)
│   ├── check_agent.py           #   Dev-maintenance-bot knowledge/spec parity
│   ├── check_bootstrap.py       #   Seed BPMN deployable by Operaton
│   ├── compose_config.py        #   Compose matrix: every overlay combo renders
│   └── yaml_lint.py             #   YAML syntax + structure validation
├── 01-specs/                    # Layer 1: Spec compliance (no containers)
│   └── validate_specs.py        #   Compose files match specs/
├── 02-contracts/                # Layer 2: Contract validation (no containers)
│   └── validate_contracts.py    #   contracts/ rules (env, ports, health, networks, security)
├── 03-smoke/                    # Layer 3: Smoke tests (requires running stack)
│   └── run.py                   #   HTTP endpoints, container health
├── 04-integration/              # Layer 4: Cross-service integration contracts
│   └── run.py                   #   DB provisioning, pgbouncer, SSO issuer, live AI proxy
│                                #   (skips when a participating service isn't running)
├── 05-e2e/                      # Layer 5: E2E tests — SSO/OIDC flows (requires running stack)
│   └── run.py                   #   Zitadel login flow, SSO reuse, logout, app checks,
│                                #   portal landing page + security-header contract
├── 06-security/                 # Layer 6: Security audit (no containers)
│   └── audit.py                 #   Exposed ports, secrets, TLS, privileges
├── 07-bench/                    # Layer 7: Benchmark (optional, manual)
│   └── run_bench.py             #   Memory + p50/p99 latency per tier → docs/perf/
└── 08-k8s/                      # Layer 8: Kubernetes deployment health (requires kubectl)
    ├── run.py                   #   Layer 8 runner
    ├── check_cluster.py         #   ArgoCD app sync/health, node readiness
    ├── check_pods.py            #   Pod health (Running, CrashLoopBackOff, restarts)
    ├── check_deployments.py     #   Deployment + StatefulSet readiness
    ├── check_images.py          #   Image registry validation (no legacy registries)
    ├── check_ingress.py         #   Ingress LB address + TLS, PVC binding
    ├── check_services.py        #   Service endpoints
    └── check_keycloak.py        #   Keycloak + OIDC discovery + OAuth2 Proxy
```

## Specs (`specs/`)

Declarative YAML files describing the **expected state** of each service:

```yaml
services:
  postgres:
    image: postgres:17-alpine
    compose_file: docker-compose.yml
    required: true
    host_ports: []          # internal only
    healthcheck: true
    networks: [opensme-net]
    volumes:
      - postgres-data:/var/lib/postgresql/data
    env: [POSTGRES_PASSWORD, POSTGRES_USER, POSTGRES_DB]
    traefik_labels: false
    resource_limits:
      memory: 4G
```

## Contracts (`contracts/`)

YAML files defining **rules** the compose files must satisfy:

```yaml
name: port-exposure
description: Internal services must not expose host ports
severity: error
rules:
  - type: no-host-ports
    services: [postgres, redis, memcached, ...]
```

## Running

```bash
# Install dependencies
pip install -r tests/requirements.txt

# Run all static layers (no running stack needed)
python3 tests/run.py --static

# Run specific layers
python3 tests/run.py --layer 0,1,2

# Run smoke tests (requires running stack)
python3 tests/run.py --smoke --domain opensme.graphwiz.ai

# Run cross-service integration contracts (requires running stack, same
# COMPOSE_FILE selection as the stack)
COMPOSE_FILE="docker-compose.yml:idm/zitadel.yml:..." python3 tests/04-integration/run.py

# Run security audit
python3 tests/run.py --security

# Run e2e tests (requires running stack; SSO flows need ZITADEL_ADMIN_PASSWORD
# from .env and access to the running zitadel container for the machine PAT,
# or a pre-registered app via E2E_OIDC_CLIENT_ID)
python3 tests/run.py --e2e --domain opensme.local

# Run k8s deployment tests (requires kubectl + running cluster)
python3 tests/run.py --k8s

# Run everything
python3 tests/run.py --domain opensme.graphwiz.ai
```

## Makefile integration

```bash
make lint          # Layer 0: compose-check + env-check + secret-scan + perf + boot contracts + compose matrix
make specs         # Layer 1: spec validation
make contracts     # Layer 2: contract validation
make test-static   # Layers 0-2 (all static checks)
make container     # container health table (requires stack)
make smoke         # Layer 3: HTTP smoke (requires stack)
make integration   # Layer 4: cross-service wiring (requires stack)
make e2e           # Layer 5: run.py + logout.py + session_lifecycle.py + `make saga`
make security      # Layer 6: security audit
make bench         # Layer 7: live benchmark (optional)
make test          # Layers 0-4 (static + container + smoke + integration; integration self-skips)
make test-all      # Layers 0-6 (full suite incl. e2e)
```

## Integration contracts (`04-integration/`)

Where Layer 3 checks single-service reachability from the host, Layer 4
verifies the **wiring between services** — probed from inside the compose
network or through the edge:

1. **Databases** — postgres provisions a database for every DB-backed
   service that is running (mirrors `postgres-init/00-create-databases.sql`)
2. **pgbouncer** — answers the PostgreSQL protocol inside the network
   (DNS + pool, probed via `pg_isready` from the postgres container)
3. **SSO issuer consistency** — `OC_OIDC_ISSUER` from opencloud's runtime
   env equals the issuer zitadel actually serves via OIDC discovery
4. **AI proxy round-trip** — `POST /api/ai/chat` succeeds end-to-end
   against the live llama.cpp backend (the deployed twin of the in-process
   contract tests in `portal/src/main.rs`)

Every check skips gracefully when a participating service isn't running,
so the layer stays meaningful for any `COMPOSE_FILE` subset.

## Portal unit tests (`portal/`)

The Rust portal ships its own test suite — in-process HTTP contract tests
(tower `oneshot`, no sockets), a mock OpenAI-compatible upstream for the AI
proxy, and property-based tests (proptest) for the announcements parser and
HTML escaping:

```bash
cd portal && cargo test     # 21 tests — runs in CI's code-quality job via `make lint-code`
```

## Adding new specs

1. Add a service to the appropriate `specs/*.yml` file
2. Run `python3 tests/01-specs/validate_specs.py` to verify
3. Add the service to relevant `contracts/*.yml` files (ports, health, networks, security)

## Adding new contracts

1. Create or edit a `contracts/*.yml` file
2. Add a rule with a supported type (see `contracts/README.md`)
3. Run `python3 tests/02-contracts/validate_contracts.py` to verify
4. If a new rule type is needed, add a handler in `tests/02-contracts/validate_contracts.py`

## E2E tests (`05-e2e/`) — SSO & friends

Exercises real user journeys over HTTP against a running stack. No browser
needed — logins are performed programmatically through the Zitadel v2
Session API (the same API the hosted Login v2 UI uses), so the suite works
against the React-based Login v2 UI where form scraping cannot.

What is covered (full story catalog with Given/When/Then:
[docs/E2E-JOURNEYS.md](../docs/E2E-JOURNEYS.md)):

1. **IdP discovery** — `/.well-known/openid-configuration` + JWKS
2. **Portal** — landing page, `/health`, `/api/services` (plus: every
   advertised service must actually answer — the catalog never lies),
   security-header contract on 200s AND 404s, plain-HTTP → HTTPS redirect
   (CSP, nosniff, frame-deny, referrer-policy — mirrors the
   middleware contract-tested in `portal/src/main.rs`), announcements
   consumer schema, AI card gating (hidden unless `AI_API_URL` set)
3. **SSO login flow** — authorization-code + PKCE, logged in via the v2
   Session API (`POST /v2/sessions` + password check + auth-request
   finalize) using the seeded login-client PAT. A throw-away OIDC app
   (`e2e-sso` project) is bootstrapped via the Zitadel Management API
   using the seeded machine-user PAT (`docker compose cp
   zitadel:/machinekey/pat -`) and removed afterwards; leftover apps
   from crashed runs are cleaned up (bootstrap is idempotent)
   **+ ID-token verification**: the issued token's RS256 signature is
   verified against the live JWKS (pure-stdlib RSA), and its
   iss/aud/exp/iat/nonce/sub claims are checked
4. **Single sign-on** — a second authorize is finalized with the existing
   IdP session; no second credential check
5. **Logout** — the session is terminated (`DELETE /v2/sessions/{id}`) and
   the dead session must be rejected (portal-driven variant with single
   sign-out to OpenCloud: `logout.py`, item 33)
6. **App reachability** — opencloud, synapse, notes, paperless, ticketing,
   cms, store, webmail (when running)
7. **SSO wiring** — synapse `/_matrix/client/v3/login/sso/redirect` and
   SOGo webmail must redirect to the IdP, not to their own login pages
8. **Identity lifecycle** — a throwaway identity is provisioned via the
   IdP API (create-with-password), completes the full SSO flow, is
   deleted, and the dead login is rejected (idempotent: leftovers are
   purged first)
9. **Operator broadcast** — `tests/05-e2e/broadcast.py` recreates the
   portal with a `PORTAL_ANNOUNCEMENTS` override (one benign + one XSS
   payload), asserts the notice is visible AND escaped, then withdraws
   it and asserts silence. Run by CI after the main suite.
10. **Files & collaboration** — opencloud web UI loads, unauthenticated
   WebDAV is denied (auth wall), and the IdP wiring of the cloud's OIDC
   client is probed honestly (warn when unregistered)
11. **Resilience** — `tests/05-e2e/resilience.py` bounces postgres
   (IdP + portal must reconnect) and stops zitadel (portal + cloud must
   degrade, not fall over; the IdP must recover). CI runs it after the
   main suite.
12. **Backup** — `tests/05-e2e/backup.py` runs `scripts/backup.sh` and
   audits the artifacts: non-empty gzip'd SQL dump with real tables
   (IdP database included), valid Traefik tar, full gzip CRC. CI runs it
   after the main suite.
13. **Session isolation** — two provisioned coworkers hold distinct IdP
   sessions; A's logout sticks while B's session still finalizes
   (part of the main suite)
14. **Exposure** — `tests/05-e2e/exposure.py`: published host ports are
   exactly the documented surface, stateful services refuse host
   connections, traefik's management API isn't routed publicly. CI runs
   it after the main suite.
15. **Runtime truth** — `tests/05-e2e/runtime_truth.py`: running images
   match the compose declaration (no drift) and nothing runs on a
   mutable `:latest` tag. CI runs it after the main suite.
16. **Local AI live** — `tests/05-e2e/ai_journey.py` recreates the
   portal with `AI_API_URL` against a stdlib OpenAI-compatible mock:
   card appears, round trip answers, upstream contract proven from the
   mock's request log, empty questions rejected, stack restored. CI runs
   it after the main suite.
17. **Burst** — `tests/05-e2e/burst.py`: 320 concurrent requests across
   the portal surfaces, zero 5xx, p95 within budget, catalog consistent
   under load. CI runs it after the AI journey.
18. **Hygiene** — `tests/05-e2e/hygiene.py`: no placeholder credentials
   in any running container env, the IdP rejects anonymous management
   writes, the cloud rejects forged bearer tokens. CI runs it after the
   burst journey.
19. **Persistence** — `tests/05-e2e/persistence.py`: `up -d` churns
   nothing, a marker row survives a full `down` + `up -d` round trip
   (volumes hold state), and the stack is functional again afterwards.
   CI runs it last because it restarts the whole stack.
20. **Intercom + cloud attachments** — `tests/05-e2e/intercom.py`: the
   intercom card is always on, notes are validated (blank/oversized →
   400) and rendered XSS-escaped; attaching a cloud file by URL yields
   REAL metadata (name from Content-Disposition, size, type) against a
   real stand-in container on the compose network; the SSRF guard holds
   (foreign hosts/ports, IP literals, link-local rejected); hostile
   filenames render escaped. CI runs it before persistence.
21. **Portability (bring your own domain)** — `tests/05-e2e/portability.py`:
   the portal MOVES to `portal.meine-firma.test` by env alone (labels
   re-interpolate at recreate), the old domain stops routing, the
   derived intercom default allowlist `cloud.<domain>` follows (stand-in
   aliased on the compose network), and rollback restores the original.
22. **Observability & log hygiene** — `tests/05-e2e/observability.py`
   (read-only): healthchecked containers report healthy, secret-shaped
   `.env` values never appear in the last 4000 log lines of any opensme
   container, portal logs are RFC-3339 structured (tracing format).
23. **Idempotency (second boot converges)** — `tests/05-e2e/idempotency.py`:
   runs `scripts/demo.sh` a SECOND time — `.env` stays byte-identical,
   an OIDC app provisioned before the boot still exists exactly once,
   the re-seeded automation PAT authenticates, stack healthy after.
   CI runs it before persistence (which restarts the stack anyway).
24. **Disaster recovery drill** — `tests/05-e2e/restore_drill.py`:
   runs `backup.sh` (with a marker database buried in the dump), then
   DESTROYS the marker database for real, restores via the interactive
   `restore.sh` ("yes" on stdin) and verifies the marker row comes back
   byte-for-byte and the stack re-boots healthy. Run it LAST.
25. **Transport & header semantics** — `tests/05-e2e/transport.py`:
   plain http redirects to https on every public hostname, the portal
   answers with the security-header contract (nosniff, frame-deny,
   referrer-policy, CSP) on page AND API, 404s leak no framework
   internals, the traefik dashboard demands basic auth, foreign vhosts
   get nothing.
26. **Accessibility & document hygiene (SSR)** —
   `tests/05-e2e/accessibility.py`: parses the landing page with the
   stdlib HTML parser — lang/viewport/title present, images carry alt,
   form controls are labelled (aria or label-for), no inline event
   handlers, one h1 with no skipped heading levels, links/buttons have
   accessible names. No browser, no JS — pure document truth.
27. **Hostile input (renderer safety)** — `tests/05-e2e/hostile_input.py`:
   event-handler injection payloads render strictly escaped in the
   intercom list, hostile attachment URLs (javascript:, data:,
   credentials, port bypasses) are rejected, unicode round-trips
   byte-identically, API serves raw truth vs page escaped markup,
   list stays bounded at 50.
28. **Rolling recreate (deploy under load)** —
   `tests/05-e2e/rolling_recreate.py`: recreates the portal while a
   traffic poller keeps probing — all failures confined to the
   recreate window (small 5xx budget), tail of the run 100% ok, stack
   healthy and content intact afterwards.
29. **Rate limiting (edge abuse resistance)** —
   `tests/05-e2e/rate_limit.py`: middleware declared AND mounted,
   100 sequential requests never punished, a 1000-request flood trips
   429s without a single 5xx, and the bucket refills so ordinary
   traffic flows freely again. Pins the repeated-flag wiring fix by
   measuring.
30. **TLS contract (crypto edge)** — `tests/05-e2e/tls_contract.py`:
   legacy TLS refused, TLS 1.2/1.3 connect, HSTS header present with a
   year-long max-age (pinned by a portal unit test), certificate
   inside its validity window.
31. **Secret surface (least privilege)** —
   `tests/05-e2e/secret_surface.py`: walks the RUNNING containers'
   real environments — the edge carries no secret-shaped vars, the
   portal never sees the DB password, the machinekey has exactly one
   holder, `.env` is mounted into nothing, dashboard creds are hashed.
32. **Concurrent writers (store correctness)** —
   `tests/05-e2e/concurrent_writers.py`: 20 parallel intercom posts
   all land, no lost updates, concurrent readers always see valid
   JSON, the page carries the surviving notes.
33. **Portal logout & single sign-out (LO-1)** —
   `tests/05-e2e/logout.py`: signs in through the v2 Session API, plants
   the ID token in the portal's `OPENSMESESSION` cookie (what an
   authenticated browser carries) and drives the portal's own logout
   contract — `POST /api/logout` clears the cookie and answers a 303 to
   the IdP end-session endpoint with `id_token_hint`, the browser lands
   on `/logged-out`, the shared IdP session is dead (replay rejected),
   and a second relying party standing in for OpenCloud is signed out
   too. CI runs it after the main suite.
34. **Session lifecycle (Epic S)** —
   `tests/05-e2e/session_lifecycle.py`: the three properties that make a
   session a session — S1 every credential is time-bounded and the
   `expires_in`/`exp`/`iat` bounds agree end to end, S2 `offline_access`
   is honored (refresh grant mints a NEW access token with a rotated
   refresh token, no re-login, still live at userinfo), S3 ONE IdP
   session covers TWO relying parties (portal app + OpenCloud client).
   CI runs it after the main suite.
35. **Webmail page contract** — `tests/05-e2e/webmail.py`: SOGo serves
   200 with login markup and every asset the page references under
   `/SOGo.woa/WebServerResources/` resolves (a blank page fails even
   when HTML and the SSO redirect look healthy). Local/operator
   journey: the demo stack ships no SOGo, so CI never runs it.
36. **Saga navigation smoke** — `tests/05-e2e/saga_smoke.py`: every
   portal tile leads to a URL that answers ("no tile to nowhere"),
   `/api/feeds` degrades to empty columns instead of erroring, and
   configured-but-undeployed catalog functions skip visibly. CI runs it
   after the main suite; the full blueprints run via `make saga`.
17. **Mailcow (full mail server)** — `tests/05-e2e/mailcow_journey.py`
   drives the vendored mailcow-dockerized submodule via
   `scripts/mailcow.sh`: admin UI through the openSME Traefik, SMTP
   announce on :25, STARTTLS submission, IMAPS, a REAL delivery round
   trip (API-provisioned mailbox → authenticated SMTP → message in
   INBOX over IMAPS, pure stdlib), SOGo webmail, and proof that the
   REST API is never served at the public edge. Skips cleanly when the
   submodule/stack isn't provisioned; dedicated CI workflow
   (`.github/workflows/mailcow.yml`) pays for the heavy image pull.

Configuration (env vars win over `.env`):

| Variable | Purpose |
|---|---|
| `ZITADEL_ADMIN_PASSWORD` | admin credentials for the login flow (`.env`) |
| `E2E_ZITADEL_LOGIN_NAME` | login name (default `zitadel-admin`) |
| `E2E_ZITADEL_PAT` | machine-user PAT (skip the `docker compose cp`) |
| `E2E_ZITADEL_LOGINCLIENT_PAT` | login-client PAT (skip the `docker compose cp`) |
| `E2E_OIDC_CLIENT_ID` / `_SECRET` | pre-registered OIDC app (skip bootstrap) |
| `E2E_INSECURE=1` | disable TLS verification |

The seeded credentials come from `zitadel setup --steps /steps.yaml`
(`idm/zitadel/steps.yaml` — v4 does not pick up the machine/PAT seeding
from env vars alone): it creates the `opensme-automation` machine user
(IAM_OWNER, PAT at `/machinekey/pat`) and the `opensme-login-client`
service user (IAM_LOGIN_CLIENT, PAT at `/machinekey/login-client.pat`).

Local demo conveniences (domains under `.local`/`.localhost`/`.test`):
unresolvable hostnames resolve to loopback (reaches local Traefik, no
`/etc/hosts` edits) and TLS verification is off (self-signed demo certs).

Skips don't fail the run; flow failures (bad login, missing code, session
surviving logout) do.
