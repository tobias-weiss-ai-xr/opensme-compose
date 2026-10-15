# Benchmark Run

_generated 2026-10-15T08:53:07+00:00_ by `tests/07-bench/run_bench.py` — live stack present._

## Memory (docker stats --no-stream)

| Container | Memory | CPU |
|-----------|--------|-----|
| file-share-dockerized-upload-1 | 39.84MiB / 15.13GiB | 0.01% |
| gha-runner-local | 75.79MiB / 15.13GiB | 0.00% |
| gha-runner-sogo6-server | 99.66MiB / 15.13GiB | 0.01% |
| opensme-memcached | 1.734MiB / 128MiB | 0.01% |
| opensme-opencloud | 114.8MiB / 1GiB | 0.30% |
| opensme-portal | 2.199MiB / 256MiB | 0.00% |
| opensme-postgres | 105.1MiB / 512MiB | 0.08% |
| opensme-redis | 10.51MiB / 256MiB | 0.13% |
| opensme-traefik | 42.55MiB / 256MiB | 0.00% |
| opensme-zitadel | 113.8MiB / 1GiB | 0.34% |
| sogo6-redis | 5.598MiB / 256MiB | 0.15% |
| sogo6-server | 0B / 0B | 0.00% |

## HTTP latency THROUGH Traefik (ms)

| Target | p50 | p95 | p99 | samples |
|--------|-----|-----|-----|---------|
| portal | 0.5 | 0.7 | 0.9 | 30 |
| opencloud | 0.5 | 0.7 | 0.9 | 30 |
| paperless | 0.6 | 0.7 | 0.7 | 30 |

## Boot baseline (Created->Started, ms)

- opensme-postgres: 907
- opensme-traefik: 11298
- opensme-portal: 1332

## Notes

- Memory/latency vary by host; pin host+commit for before/after.
- CI runs only the static budget subset (`tests/00-static/check_perf.py`); live numbers are advisory.
