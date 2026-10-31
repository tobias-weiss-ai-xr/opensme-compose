# openSME — Blackbox monitoring blueprint
# =======================================

What this is
------------
A reusable, deployment-agnostic way to monitor **every component of an
openSME instance from the outside** with Prometheus + Blackbox exporter.
Probes always go through the public entry point (the reverse proxy), so the
checks catch the failure class that actually bit the live instance on
2026-10-27:

* apex `opensme.graphwiz.ai` and `home.opensme.graphwiz.ai` returned **502**
  because containers were stuck and two Traefik **file-routers pointed at
  stale container IPs** — a static 200 hit-test at install time would never
  have caught that. A blackbox probe from *outside* monitors it forever.

Files
-----
```
monitoring/blackbox/
├── blackbox.yml               Blackbox exporter modules (adjust markers)
├── blueprint.yml              ★ THE "adjust here" file — target inventory
├── generate-scrape.py         renders blueprint + env → Prometheus config
├── compose.yml                standalone monitor (profile: monitoring)
├── prometheus-rules-opensme.yml  alerts (component down / slow)
└── README.md                  this file
```

How the blueprint resolves targets
----------------------------------
Every entry becomes `https://<fqdn><path>` where

    fqdn = <full>                                      (explicit domain)
         = <sub>.<MONITOR_SUBDOMAIN_BASE><OPENSME_DOMAIN>   (suite scheme)

* `OPENSME_DOMAIN` — apex / marketing domain (`opensme.graphwiz.ai`).
* `MONITOR_SUBDOMAIN_BASE` — the shared prefix for suite services:
    - `""`      → `auth.<domain>`, `cloud.<domain>`, `webmail.<domain>` … (vanilla compose scheme)
    - `"home."` → `auth.home.<domain>`, `cloud.home.<domain>` … (the live homelab scheme)
* per-service `env:` hint — a `<NAME>_DOMAIN` env var that, when set,
  overrides that single entry (e.g. `PORTAL_DOMAIN`, `OC_DOMAIN`).
* `enabled: false` — an entry you do not run (e.g. helpdesk/Nosdesk).

The generator reads the **same `.env` (or process env) as the compose
chain**, so monitoring always matches the deployed topology — no separate
hardcoded domain list.

Modules (blackbox.yml)
----------------------
| module        | passes when                       | use for                    |
|---------------|-----------------------------------|----------------------------|
| `http_avail`  | 2xx, 3xx, 401                     | routing/availability       |
| `http_200`    | 2xx only (no redirect)            | deep checks (`/health`)    |
| `http_content`| 200 + your marker in the body    | marketing site (anti-empty)|
| `tcp_connect` | TCP dial succeeds                 | SMTP / submission / IMAPS  |
| `icmp`        | host answers ping                 | hosts (opt-in)             |

The `http_content` marker is a per-deployment string — **edit
`fail_if_body_not_matches_regexp` in `blackbox.yml`** to something unique to
your marketing page so a 200 error page still alerts.

Usage
-----
1. Render the probe list (sanity check that every URL is yours):
   ```sh
   python3 monitoring/blackbox/generate-scrape.py --env-file .env --print
   ```

2. Render scrape config:
   * standalone Prometheus (default `--mode full`) →
     `-o monitoring/blackbox/standalone-prometheus.yml`
   * append into an existing Prometheus (`--mode fragment`) and import the
     YAML block (see "Central integration" below).

3a. **Standalone** (your own Prometheus + Blackbox):
    ```sh
    python3 monitoring/blackbox/generate-scrape.py --env-file .env \
        -o monitoring/blackbox/standalone-prometheus.yml
    docker compose -f monitoring/blackbox/compose.yml --profile monitoring up -d
    ```
    Prometheus → http://127.0.0.1:9090, Blackbox → :9115,
    alerts `OpenSMEComponentDown` / `OpenSMEComponentSlow`.

3b. **Central integration** (a fleet Prometheus that already runs a
    Blackbox exporter, e.g. the homelab central instance):
    ```sh
    # BLACKBOX_ADDR must be the exporter address AS SEEN BY prometheus
    BLACKBOX_ADDR=198.51.100.20:9115 \
      python3 monitoring/blackbox/generate-scrape.py --env-file .env --mode fragment
    ```
    Append the emitted `scrape_configs` entries to your existing
    `prometheus.yml`, deploy, and check
    `GET /api/v1/targets` for `"health":"up"`.

What "all components" means
---------------------------
The default inventory covers every surface of a standard openSME suite:
marketing/apex, portal UI + the Rust `/health` deep check, webmail
([`/en`](landing page) deep check — not `/SOGo`, which 307-chains to a 404),
Zitadel, OpenCloud, Stalwart (TCP :25/:143/:587 probe the mail service; the
Webadmin HTTP listener is operator-only and `enabled: false` by default),
Notes, Synapse/Element, the Matrix federation client API deep check,
Paperless, Operaton/Camunda, LimeSurvey, Collabora, Invoice Ninja, the
container registry and the landing site.

Adjustments you will likely make
--------------------------------
* your domains (`.env` / env vars — see resolution rules above)
* `MONITOR_SUBDOMAIN_BASE` if your suite lives under `svc.<domain>`
* the `http_content` marker in `blackbox.yml`
* enable/disable entries via `enabled:` in `blueprint.yml`
* alert thresholds in `prometheus-rules-opensme.yml`
* `BLACKBOX_ADDR` when integrating with a central Prometheus / non-compose
  exporter address
