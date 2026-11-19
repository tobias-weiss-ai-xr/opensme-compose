# Performance & Efficiency — openSME

How the stack is sized, tuned, and kept efficient across the three VPS tiers
(soho 4c/8G, small 8c/24G, medium 16c/48G). Companion to
[docs/perf/baselines.md](perf/baselines.md) (numbers) and
[docs/perf/README.md](perf/README.md) (matrix + benchmark protocol).

## Principles

1. **Reservations are the guarantee; limits cap spikes.** Tier mode is
   *reservations-first*: we budget what the scheduler must guarantee, let CPU
   burst where safe.
2. **Enforce, don't hope.** `tests/00-static/check_perf.py` fails CI when a
   service regresses: missing limits, no log cap, no healthcheck, or a tier
   that blows its budget.
3. **Small to fit, strip to suit.** Each tier lists *what may run*; unlisted
   services are disabled, not just resized.
4. **Measure, don't assert.** `scripts make bench` records memory + latency
   before/after on a live host; CI keeps the static invariants.
5. **Measure at the edge.** Request latency is read from the Traefik edge
   access log (trailing `NNms` field = bytes delivered to the browser) —
   never from OpenCloud's own access log, whose `duration=` field reports
   the age of the upstream TCP connection (Traefik keep-alive pool), not
   request latency (proven live 2026-11-18: client 0.03-0.35 s vs
   OpenCloud-logged 7.4-53.6 s). The cron suite monitor
   (`scripts/suite-monitor.sh`, check C5) scans the edge log;
   `tests/00-static/check_monitoring.py` (M9) guards the rule.

## Per-tier runtime parameters

### PostgreSQL (single instance, all databases)

| Setting | soho | small | medium |
|---------|------|-------|--------|
| shared_buffers | 128MB | 256MB | 1GB |
| effective_cache_size | 512MB | 1GB | 3GB |
| work_mem | 8MB | 16MB | 32MB |
| maintenance_work_mem | 64MB | 128MB | 256MB |
| autovacuum_work_mem | 64MB | 128MB | 256MB |
| max_connections | 50 | 100 | 200 |
| max_wal_size | 1GB | 2GB | 4GB |
| shm_size | 256MB (base) | 256MB | 256MB |
| checkpoint_completion_target | 0.9 | 0.9 | 0.9 |
| bgwriter_lru_maxpages | 100 | 100 | 200 |

`random_page_cost=1.1`, `effective_io_concurrency` 100/200/200 apply
throughout. Settings live in `profiles/{soho,small,medium}.yml` (Compose CLI
args so profiles merge cleanly). `shared_buffers` ≤ ~1/4 of the container
limit keeps the kernel cache useful.

### Redis / Memcached (caches)

- `--maxmemory` is **below** the container memory limit (headroom for
  allocator overhead + AOF buffers): soho 128M in 192M, small 1G in 1.5G,
  medium 2G in 3G.
- `allkeys-lru` + `lazyfree-lazy-eviction` / `lazyfree-lazy-expire` — eviction
  is lazy so latency stays flat under pressure.
- SOHO runs cache-only (`--save "" --appendonly no`); Small/Medium keep
  snapshots + AOF `everysec` for crash safety.
- Memcached slabs are only slightly under the limit (`-m 96/384/1280` in
  128M/512M/1.5G) so the slab allocator never OOMs inside the limit.

### PgBouncer

Transaction pooling in front of Postgres. Pool sizes track
`max_connections`: small `default_pool=15 / max_client=100 / max_db=30`,
medium `default 25 / max_client 200 / max_db 50`. Disabled on SOHO.

### Traefik

- Healthcheck via built-in `traefik healthcheck` (ping endpoint) so
  dependents/proxied reachability is truthful.
- `compress` middleware on web+websecure (gzip/brotli) cuts asset payloads.
- HTTP/3 enabled; rate-limit 100/s burst 200 for the dashboard/edge.
- `cap_drop: ALL` + `NET_BIND_SERVICE` only; `read_only` root with tmpfs
  `/tmp`; `no-new-privileges`; `init: true`.

## Boot ordering

Every overlay waits on its stores via `depends_on: condition:
service_healthy`:

- stalwart → postgres; sogo → postgres + stalwart
- opencloud → postgres; paperless-ngx → postgres + redis + gotenberg
- invoiceninja → postgres + redis; notes → postgres + redis + y-provider
- element → synapse; portal → postgres

`stop_grace_period`: postgres 120s, stalwart 120s, sogo/opencloud/paperless/
minio 60s — orderly flush, no forced kills.

## Hardening defaults (all services)

- `init: true` (zombie reaping, PID1 hygiene)
- `security_opt: no-new-privileges:true`
- `cap_drop: ALL` (+ needed caps) on traefik, pgbouncer, redis, memcached,
  stalwart, sogo, minio, gotenberg, portal
- `read_only` + tmpfs `/tmp` on traefik and portal; tmpfs scratch on
  paperless, gotenberg, tika, sogo, opencloud
- Logging caps on **every** service (`json-file`, 50m×3)

## Known gaps / future

- **Live benchmark evidence** for this repo is performed on a deployed host
  (`make bench`) — record results into `docs/perf/benchmark-run.md` per the
  protocol; CI asserts the static subset only.
- Image **digest pinning** is documented in `.env.example`; pinning to a
  specific digest is the operator's choice for reproducibility.
- Effi gains beyond 0.8/3.4/8G reserved require *stripping* (matrix above),
  not further resizing.
