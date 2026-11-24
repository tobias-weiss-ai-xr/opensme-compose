# opensme-knowledge expansion + agent integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grow the embedded runbook KB (`opensme-knowledge/*.json`) from 10 to ~19 services with ~40 incident-derived entries, bump `kb_version`, tighten Layer-0 validation, and make the agent actually consume the `requires-consent` flag.

**Architecture:** Pure JSON content (Part A) + Python Layer-0 lint (Part B) + small Go agent changes (Part C). Content is mined from `~/af-camps` campaign logs and this repo's recent fix commits (all entries verified against commit diffs or live log evidence). Go changes are minimal and follow existing patterns.

**Tech Stack:** JSON (schema v1), Go 1.22 (embed + knowledge/healer/api packages), Python 3.12 (check_agent.py), docker compose.

**Verification constraints:** `go` is NOT installed locally and no build host is reachable — Go changes are verified here by careful review only; `go test` runs in CI (`.github/workflows/ci.yml` `go test ./...`). Python/JSON parts are fully verifiable locally with `python3 tests/00-static/check_agent.py`. Do NOT attempt local `cargo`/`go` builds.

---

## File structure

**Create (new KB service files):**
- `opensme-knowledge/redis.json`
- `opensme-knowledge/pgbouncer.json`
- `opensme-knowledge/memcached.json`
- `opensme-knowledge/synapse.json`
- `opensme-knowledge/radicale.json`
- `opensme-knowledge/litellm.json`
- `opensme-knowledge/limesurvey.json`
- `opensme-knowledge/notes.json`
- `opensme-knowledge/calendar.json`

**Modify (existing KB files — add runbooks + bump kb_version 2026.10→2026.11):**
- `opensme-knowledge/common.json` (+ netns passenger lifecycle rule)
- `opensme-knowledge/opencloud.json` (+5: O1–O5)
- `opensme-knowledge/sogo.json` (+3: G1–G3)
- `opensme-knowledge/stalwart.json` (+4: W1–W4)
- `opensme-knowledge/invoice-ninja.json` (+3: I1–I3)
- `opensme-knowledge/traefik.json` (+3: T1–T3)
- `opensme-knowledge/zitadel.json` (+2: Z1–Z2)
- `opensme-knowledge/postgres.json` (+1: PO1)
- (operaton.json, paperless.json: bump kb_version only)

**Modify (validation + agent):**
- `tests/00-static/check_agent.py` — CORE_SERVICES align, kb_version consistency check
- `internal/knowledge/knowledge.go` — `RequiresConsent()` method + flag const
- `internal/knowledge/knowledge_test.go` — tests for the above
- `internal/api/server.go` — PrintStatus: remediation steps + `[consent]` markers
- `main.go` — tighten `//go:embed opensme-knowledge` → `//go:embed opensme-knowledge/*.json`
- `docs/superpowers/plans/2026-11-23-opensme-knowledge-expansion.md` (this plan)

---

## Part A — Knowledge content

### A1. New service file `opensme-knowledge/redis.json`

All three entries verified (commits a772e78/9490aef/9efa115, 06290be, e1e298e). Topology fact to bake into every netns entry: postgres is netns owner of the "infra quartet" — `pgbouncer`, `redis`, `memcached` attach via `network_mode: "service:postgres"` (docker-compose.yml:281/321/348); recreating postgres without the passengers orphans them in a dead loopback-only netns.

R1 netns orphan: symptoms `connection refused`, `redis:6379`, `netns`, `sse stream went silent`, `postgres recreate`; remediate `docker compose up -d pgbouncer redis memcached`; verify `docker exec opensme-redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning ping` → PONG. `flags: [requires-consent]`.

R2 REDIS_HOST lost on recreate: symptoms `127.0.0.1`, `redis connection refused`, `laravel`, `crash loop`, `REDIS_HOST`; remediate: declare `REDIS_HOST: redis` + `REDIS_PASSWORD: ${REDIS_PASSWORD}` in BOTH `invoiceninja-fpm` and `invoiceninja-worker` env blocks (services/invoice-ninja.yml), then `docker compose --profile invoice up -d --force-recreate` profile.

R3 celery broker auth: symptoms `kombu`, `AuthenticationError`, `celery worker offline`, `500 on /api/v1.0/documents/`; remediate: set `DJANGO_CELERY_BROKER_URL: redis://:${REDIS_PASSWORD}@redis:6379/0` in services/notes.yml env of notes-backend (django-configurations needs the `DJANGO_` prefix; plain `CELERY_BROKER_URL` is silently ignored), recreate notes-backend.

### A2. New service file `opensme-knowledge/pgbouncer.json`

P1 verified (a772e78 + compose topology). Symptoms `connection refused`, `pgbouncer:6432`, `postgres bounce`, `too many clients`. Diagnosis: netns passenger of postgres. Remediate `docker compose up -d pgbouncer` after any postgres recreate; check `docker compose ps`; confirm a consumer reconnects. `flags: [requires-consent]`.

### A3. New service file `opensme-knowledge/memcached.json`

M1 verified (a772e78 + compose). Symptoms `connection refused`, `memcached:11211`, `postgres bounce`, `netns`. Diagnosis: third netns passenger of postgres, runs `memcached -m 2048 -c 4096 -t 4` in the shared netns. Remediate `docker compose up -d memcached` after postgres recreate. `flags: [requires-consent]`.

### A4. New service file `opensme-knowledge/synapse.json`

S1 verified: legacy API prefixes removed in Synapse 1.162.0 — `/r0/` rejected, `/r3/` nonexistent; only `/_matrix/client/v3/` answers. Symptoms `unrecognized request`, `legacy prefixes`, `r0`, `r3 is unrecognized`, `404`; remediate: point booking-notify.sh / ai-room-bot / broadcast logic at v3 endpoints, keep txnId-idempotent sends. No consent.

S2 verified (aedf115): SSO created duplicate `u_<sub>` accounts; default `localpart_template='u_<sub>'`, `allow_existing_users` unset, no `client_whitelist`. Symptoms `u_398171505348837396`, `mxid`, `two accounts`, `confirm redirect`, `zero-click sso`. Remediate: synapse-setup.sh sets `localpart_template` from Zitadel `preferred_username` with `u_<sub>` fallback, `allow_existing_users: true`, `sso.client_whitelist` = Element host; migrate: pre-bind users, deactivate orphan `u_*` accounts. `flags: [requires-consent]`.

S3 verified: register MAC pitfalls. Symptoms `429`, `retry_after_ms`, `m.room.message`, `mac mismatch`, `synapse register`. Remediate: compute register MAC over exact byte string; `docker exec -i`; build curl args in bash arrays, never `$(...)`-word-split auth headers; honor retry_after_ms with backoff. No consent.

### A5. New service file `opensme-knowledge/radicale.json`

R1 verified (b5901c5, 9490aef): calendar-radicale shares calendar's netns (`network_mode: "service:calendar"`, pinned IP 172.19.0.250 opensme-net). `docker compose up --wait calendar` recreates the owner but NOT the passenger → radicale stuck in orphaned loopback-only netns → calrs sync `tcp connect error: Connection refused (os error 111)` and edge `/dav` 502 while compose exits 0. Symptoms `connection refused`, `calendar-radicale:5232`, `502 /dav`, `netns`, `loopback`. Remediate `docker compose up -d --wait calendar calendar-radicale`, verify Last Synced + `/u/demo/vc` 200. `flags: [requires-consent]`.

R2 verified: Radicale serializes resourcetype as `<C:calendar />` (uppercase prefix + space) — `grep '<c:calendar/>'` never matches → fallthrough to MKCALENDAR → benign 409. Symptoms `MKCALENDAR`, `409`, `<c:calendar/>`, `zero calendars`, `PROPFIND`. Remediate: probe spaced/uppercase form or PROPFIND 207 + tag presence; treat 409 on existing collection as warn. No consent.

R3 inferred-but-solid: SOGo CalDAV subscription ICS fetcher is https-only and rejects private IPs; booking store exposed read-only at `calendar.<host>/dav/` behind radicale htpasswd (RADICALE_USER/PASSWORD). Symptoms `CalDAV subscription`, `https-only`, `rejects private IPs`, `calendar.<host>/dav`. Remediate: keep edge `/dav` route basic-auth; never point sogo subscription at internal http IP. No consent.

### A6. New service file `opensme-knowledge/litellm.json`

L1 verified: `docker run -e LITELLM_MASTER_KEY` with unexported var → empty → gateway 401 while verbatim rerun works. Symptoms `401`, `AuthenticationError`, `LITELLM_MASTER_KEY`, `docker run -e`. Remediate: `export LITELLM_MASTER_KEY` before `docker run -e`, guard empty key warn-not-fail. Note the anonymizer strips `key=...` from ev logs — mismatch between export and run is the fuse.

L2 verified (3e1d3fd): litellm image has no curl → old curl healthcheck permanently failing. Symptoms `curl: not found`, `healthcheck`, `unhealthy`, `crontab`. Remediate: python3 urllib healthcheck (`python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health/liveliness', timeout=5)"`); ollama image likewise has no curl — use `ollama ls`.

L3 verified (a94fa7c): litellm unreachable during boot/recreate → `APIConnectionError`, `Timeout on reading data from socket`, `ECONNREFUSED`. Remediate: treat as transient — retry 429/5xx, fall back to ollamaChat once, surface German `LlmError`; check container health before blaming the key.

### A7. New service file `opensme-knowledge/limesurvey.json`

L1 verified (T5): LS6 JSON-RPC only when `$config['RPCInterface'] = 'json'` INSIDE the returned config array; top-level `$config[...]` assignments discarded; OPcache may serve stale bytecode. Symptoms `empty body`, `/admin/remotecontrol`, `RPCInterface`, `remotecontrol`, `200`. Remediate: patch config.php inside `'config'=>array(...)`, restart web process or container (clear OPcache), pin the patch in the image/Dockerfile so recreates keep it. `flags: [requires-consent]`.

L2 verified: bad credentials → controller `die()` → 200 text/html EMPTY body; success returns 32-char session key. Symptoms `empty body`, `200 text/html`, `get_session_key`, `auth`, `false ok`. Remediate: never report OK on empty/non-JSON body; treat empty body after login as auth failure; verify session key length > 0.

L3 verified (8eda199): envelope `id: 0` treated as notification → empty body; login must use object form `{username, password}` and monotonically increasing `id >= 1`. Symptoms `id: 0`, `empty body`, `notification`, `get_session_key`. Remediate: object-form params + `++rpcId` from 1 in TS client and `scripts/limesurvey-rpc.sh`.

L4 verified: per-IP login-attempt lockout ~10 min. Symptoms `wait 10 minutes`, `locked`, `too many login attempts`, `login-attempt`. Remediate: wait out lockout, single-flight session-key caching, never hammer wrong passwords.

### A8. New service file `opensme-knowledge/notes.json`

N1 verified (ca84d46, live 2026-11-13): zitadel used to alias public `${ZITADEL_DOMAIN}` on opensme-net → embedded DNS resolved the IdP FQDN to the zitadel container inside every compose service (only plain :8080 there) → every server-side OIDC backchannel died `connection refused`. Fix: zitadel now aliases only `auth.opensme.local`; every server-side dialer pins `${ZITADEL_DOMAIN}:host-gateway` via `extra_hosts`. Symptoms `connection refused`, `/api/v1.0/callback/`, `500`, `token exchange`, `notes sso`. Remediate: ensure each OIDC-backchannel service has `extra_hosts: ["${ZITADEL_DOMAIN}:host-gateway"]`; never re-add public FQDN as zitadel alias; verify with `curl` to token endpoint from service netns. `flags: [requires-consent]`.

N2 verified (ca84d46): Zitadel JWKS at `/oauth/v2/keys`, conventional `/.well-known/.../jwks` 404s → ID-token validation 500s. Symptoms `jwks`, `404`, `id-token`, `500 on callback`. Remediate: `OIDC_OP_JWKS_ENDPOINT: https://${ZITADEL_DOMAIN}/oauth/v2/keys` (fixed in notes.yml).

N3 verified: fresh DB `relation "impress_user" does not exist` — run `python manage.py migrate --noinput` before uvicorn (impress image entrypoint never migrates).

N4 verified: default `DJANGO_CONFIGURATION=Development` appends `django_extensions` (absent from image) → ImportError at boot. Remediate: `DJANGO_SETTINGS_MODULE: impress.settings` + `DJANGO_CONFIGURATION: Production`; do NOT set settings/ package module.

N5 verified (418bdf8, live 2026-10-13): 256M limit OOM-killed Django boot (4 multiprocessing workers × ~185M RSS ≈ 750M). Symptoms `oom killed`, `SIGKILL`, `notes-backend`, `out of memory`. Remediate: keep limit ≥ 1.5G; check `docker inspect opensme-notes-backend` limits + dmesg. `flags: [requires-consent]`.

N6 verified: notes-frontend attaches `network_mode: service:notes-backend`; without `/oidc` PathPrefix router every SSO path 404s; Traefik 2.11 refuses auto-link when container defines multiple services → explicit links. Symptoms `404`, `/oidc/callback`, `notes frontend`, `sso path`, `netns`. Remediate: keep `notesapi` router `Host(notes.<domain>) && (PathPrefix(/api) || PathPrefix(/oidc))` priority 100, labels on netns-owner (backend), explicit service links.

### A9. New service file `opensme-knowledge/calendar.json`

C1 verified (388a9a9, T3): calrs 1.6.0 dashboard event-type editor stores the literal title as `location_value` (seed wrote `'Discovery Call'` instead of VC room template); calrs v1.6.0 CLI has no location flags → direct sqlite helper on volume `compose_calrs-data` `/var/lib/calrs/calrs.db` (owner 999:999, WAL). Symptoms `Discovery Call`, `location_value`, `event_types`, `calrs`, `vc room link missing`. Remediate: stop `opensme-calendar`; one-shot alpine:3.19 helper `apk add sqlite` + `UPDATE event_types SET location_value='https://vc.<domain>/room/{uid}' WHERE slug='call';` + guarded INSERT for `chat`; start calendar; restart passenger; verify rows and `/u/demo/vc` 200; never touch DB writable while app up (WAL). `flags: [requires-consent]`.

C2 verified (T3): only admin `demo` account owns event types; `testuser` (OIDC operator) has none → `/u/testuser/vc` "Event type not found". Symptoms `Event type not found`, `/u/testuser/vc`, `booking url`. Remediate: book/verify against the account owning the slug; when a user reports it, check the owning account.

C3 verified (subsumes radicale R2): calrs sync "ZERO calendars" when PROPFIND resourcetype probe mismatches. Symptoms `zero calendars`, `PROPFIND`, `resourcetype`. Cross-link to radicale R2.

### A10. Entry additions to existing files

Each entry: full verified facts held in session; all get kb_version bump to 2026.11.

- **common.json** + netns passenger lifecycle rule (cross-cutting, verified across 4 incidents: a772e78, b5901c5/9490aef, 37ae533). Symptoms `network_mode`, `netns passenger`, `orphaned in dead netns`, `up --wait`, `passenger`. When a service uses `network_mode: "service:<owner>"` (pgbouncer/redis/memcached→postgres; calendar-radicale→calendar; portal-web→portal; invoiceninja fpm/web→web; notes-frontend→notes-backend), a recreate of the owner without the passenger leaves it in a dead loopback-only netns. Always `docker compose up -d --wait <owner> <passenger>...` together. `flags: []`.
- **opencloud.json** +O1 (nix image `path escapes from parent` openat2, execfix layer /etc/passwd+group materialized; commits 317b7fa/ad3fb58/b8e468c) `flags: [requires-consent]`; +O2 (8.x→9.x idm bind, pin OC_ADMIN_PASSWORD etc.; umfragen-bot recreate via Graph API) `flags: [requires-consent]`; +O3 (082cb02 metadata prefix `user.ocis.` + empty NOTIFICATIONS_SMTP_SENDER panic on fresh demo) `flags: []`; +O4 (4e1dba9 OC_URL http vs https edge → ocis proxy 404 on /) `flags: []`; +O5 (T5-verify-sweep static IP 172.19.0.17 stolen by postgres after restart; disconnect/reconnect + restore `--alias postgres` netns alias — losing alias caused zitadel DB errors) `flags: [requires-consent]`.
- **sogo.json** +G1 (630c40e: IdP login-domain tenant `opensme.auth.home.opensme.graphwiz.ai` missing from callback allowlists → authorize 400 invalid_request; register callback path derived from ZITADEL_LOGIN_DOMAIN + both tenants in allow-redirect) `flags: [requires-consent]`; +G2 (28db03f: upstream salvoxia/sogo:latest rebuilt baking HTTP_PORT=8080 into ports.conf → webmail 502 with healthy container; pin HTTP_PORT: 80, healthcheck probes sogod not apache) `flags: [requires-consent]`; +G3 (v6 schema: table `users` not `sogo_users`/`sogo6_users`; probe `SELECT uid, cn, mail, enabled FROM users WHERE uid LIKE '%test%';` on opensme-sogo6-postgres) `flags: []`.
- **stalwart.json** +W1 (T3: ufw blocks container→host :8081 → XOAUTH2 hangs, app-password 401s; `sudo ufw allow from $DOCKER_SUBNET to any port 8081 proto tcp` (subnet from
the compose network, never a literal private range in the repo); persist in ansible web_hosts firewall role) `flags: [requires-consent]`; +W2 (550 Mailbox does not exist on multi-recipient — principals auto-provision on first SSO login; per-recipient best-effort sends) `flags: []`; +W3 (`Connection unexpectedly closed` when many RCPTs — 14 RCPTs dropped session; 5 parallel per-recipient workers, SCRIPT_TIMEOUT_MS 60s) `flags: []`; +W4 (DMARC-fail sender → test invoice filed to Junk; sieve `fileinto INvoices` via admin API `--print-sieve` from scripts/paperless-intake.sh; whitelist/SPF/DKIM sender) `flags: []`.
- **invoice-ninja.json** +I1 (015eab6/6743e5d: `artisan db:*` commands don't exist; v5 schema — companies has no `name` column, settings JSON, no company_settings table, MariaDB RETURNING unsupported; provision via SQL through opensme-invoiceninja-db container) `flags: [requires-consent]`; +I2 (e5d3eee: nginx absolutised relative `Location: /auth/oidc` against http scheme → extra hop; `return 302 https://$host/auth/oidc;`) `flags: []`; +I3 (b8e468c: healthcheck required `bootstrap/cache/config.php` which entrypoint may not generate → forever unhealthy; probe `http://127.0.0.1:80/health` via busybox wget instead) `flags: []`.
- **traefik.json** +T1 (09e8a45: demo `portal-web` router priority 100 shadowed `portal-https` (default ~32) → :443 502 against dead :3000; raise `portal-https` priority=200 in demo profile) `flags: []`; +T2 (T5: file-provider standin routers pin hardcoded IPs that daemon restarts shuffle — portal-standin→.21, webmail-standin→.7, mail-home→.22; refresh pins after daemon restart, prefer service-name DNS) `flags: [requires-consent]`; +T3 (2868284: `certresolver=letsencrypt` under profiles/system-traefik.yml doesn't exist → routes 404; re-point calendar/calendar-dav/vc-room routers at `mytlschallenge`, attach traefik-web) `flags: [requires-consent]`.
- **zitadel.json** +Z1 (048ff3c: sequential oidc_config PUTs self-clobber — absent fields reset-to-default, redirect-URIs PUT reset accessTokenType JWT→OPAQUE; one atomic PUT with all four fields) `flags: [requires-consent]`; +Z2 (8e51517: unregistered `post_logout_redirect_uri` root → end_session 400; register app root + callback paths in `POST_LOGOUT_OVERRIDE` for opencloud app, re-run registrar `--app opencloud`) `flags: [requires-consent]`.
- **postgres.json** +PO1 (T3: psql `:'pw'` NOT interpolated inside dollar-quoted DO bodies → role creation silently failed (sogo_user); use plain statements + shell-side escaping, verify `SELECT COUNT(*) FROM mail_users;` — "relation mail_users does not exist" = postgres-init/02-mail-users.sql not applied) `flags: [requires-consent]`.

### A11. Schema/version bump + validation

- [ ] Bump `"kb_version": "2026.10"` → `"2026.11"` in ALL 19 KB files (existing + new).
- [ ] Validate every file: `python3 -c` JSON parse loop; then `python3 tests/00-static/check_agent.py` (expect PASS).
- [ ] Add `opensme-knowledge` files stay PII-free: symptom strings contain no hosts/IPs/paths (verify by eyeball; check_agent.py KBLeak enforces the rest).
- [ ] Commit Part A.

---

## Part B — Layer-0 validation (check_agent.py)

### B1. Align CORE_SERVICES
`tests/00-static/check_agent.py:28-31` currently `{traefik, postgres, zitadel, stalwart, sogo, opencloud, invoice-ninja, paperless}` — diverges from Go `knowledge_test.go:87` (`+operaton, +common`) and openspec. Add **operaton** and **common** to the set. Note in the module docstring that core = the 10 in openspec dev-agent-knowledge.

### B2. kb_version consistency check
In `check_knowledge()` (after line 79), collect `doc.get("kb_version")` for every file; if the set has >1 distinct value → error `kb_version must be uniform, got {set}`. Rationale: aktualität is per-release; mixed versions mean a stale file shipped.

- [ ] Edit `tests/00-static/check_agent.py`.
- [ ] Run `python3 tests/00-static/check_agent.py` — PASS.
- [ ] Commit Part B.

---

## Part C — Go agent integration (CI-verified; no local Go)

### C1. knowledge.go — makes `requires-consent` first-class

Add to `internal/knowledge/knowledge.go` (near Runbook def, ~line 23):

```go
// FlagRequiresConsent marks mutating remediation steps that the healer must
// not run without DEV_AGENT_ALLOW_HEAL (dev-agent-knowledge: flags).
const FlagRequiresConsent = "requires-consent"

// RequiresConsent reports whether the runbook's remediation mutates state
// and therefore needs explicit consent before the healer may act on it.
func (r Runbook) RequiresConsent() bool {
	for _, f := range r.Flags {
		if f == FlagRequiresConsent {
			return true
		}
	}
	return false
}
```

Tests in `internal/knowledge/knowledge_test.go`:
- `TestRequiresConsent` — runbook with `Flags: ["requires-consent"]` → true; with `["other"]` or empty → false; case-sensitive match.
- Extend `TestRealKB` commit-style check OR add `TestAllFlaggedRunbooksSayConsent` that loads the real KB and asserts: every runbook flag is either empty or `requires-consent` (no unknown flags ship).

### C2. server.go — PrintStatus shows remediation + consent

Edit `internal/api/server.go` `PrintStatus()` (~line 765). After the existing `KB[...]` line, when `m.Runbook.RequiresConsent()`, append `[consent]` marker, and print the first remediation step prefixed `   fix:` (all steps if <3, else first + `   (+N more)`). Keep source duty line unchanged. Example shape:

```
  KB[zitadel@2026.11] migration failed|... → <diagnosis> (source zitadel.json) [consent]
     fix: Use ZITADEL_FIRSTINSTANCE_ORG_NAME + ...
```

Test in `internal/api/api_test.go` (or a new focused test): build a Store from a small in-memory map with a flagged runbook + a plain one, wire into a Server, assert output contains `[consent]` only on the flagged line and contains the `fix:` line.

### C3. main.go — tighten the embed

`main.go:24` `//go:embed opensme-knowledge` → `//go:embed opensme-knowledge/*.json`. Stops shipping `README.md`/`CONTRIBUTING.md` inside the binary; loader (`internal/knowledge.Load`) is unaffected (ReadDir still enumerates the dir; only *.json match the glob). This is safe with `go:embed` (dir glob with files matching at that level).

### C4. Healer note (documented, not coded)

Per-runbook healer *enforcement* beyond the global `DEV_AGENT_ALLOW_HEAL` gate is deliberately NOT added: the healer only executes `restart`, which is already Allow-gated, and remediation steps are advisory text — there is no action executor consuming them yet. `RequiresConsent()` (C1) + PrintStatus markers (C2) give the flag live consumers now; a future mutating action (e.g., `prune` of a specific volume) will gate on the flag. Add one sentence to `internal/healer/healer.go` package comment noting this.

- [ ] Apply C1–C4 edits (careful review; no local go toolchain).
- [ ] Sanity: `python3 -c "import json;[json.load(open(f)) for f in __import__('glob').glob('opensme-knowledge/*.json')]"` (JSON intact).
- [ ] Commit Part C with message noting CI verifies `go test`/`go build`.

---

## Self-review notes

- **Spec coverage:** KB expansion (A), validation (B), integration (C) all mapped to the user's 4 selected tracks: mine incidents (A1–A10), expand coverage (A1–A9), validate+bump (A11, B), agent integration (C).
- **Placeholders:** none — every entry references verified incident sources (commits/log files).
- **Type consistency:** `RequiresConsent()` and `FlagRequiresConsent` are defined in C1 before use in C2; `Match.Runbook` is publicly embedded so `m.Runbook.RequiresConsent()` compiles.
- **Verification gap:** Go portions verified by review only (toolchain absent); documented in header.
