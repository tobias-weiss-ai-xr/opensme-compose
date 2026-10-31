# openSME portal Web UI (Next.js)

The portal's frontend, rewritten in **Next.js 15 (App Router) + TypeScript +
Tailwind v4** in the style of the openEduSuite website, sitting in front of the
Rust axum **API** (`portal/`, the `portal` service).

```
 browser ──► traefik ──► portal-web:3000 (Next.js)
                              │  /api/* │ /health │ /metrics
                              ▼         (route handlers, runtime BACKEND_URL)
                        portal:8080 (Rust axum — SSO logout, feeds, intercom,
                                     polls, AI, metrics; the security surface)
```

Why split? The Rust backend owns the security-sensitive, e2e-tested contract
(OIDC end-session logout, SSRF-guarded attachments, Prometheus metrics). The
Next.js app renders the landing page and proxies every `/api|/health|/metrics`
request **same-origin** (`src/app/api/[[...path]]/route.ts`, runtime
`BACKEND_URL`), so CSP `connect-src 'self'` holds and the external API contract
is byte-identical.

## Development

```bash
npm ci
npm run dev        # http://localhost:3000 (backend expected on :8080)
npm test           # Vitest
npm run lint       # ESLint (next/core-web-vitals + next/typescript)
npm run typecheck  # tsc --noEmit
npm run build      # next build (standalone)
```

Runtime env: `BACKEND_URL` (default `http://localhost:8080`), plus the same
`PORTAL_*` / `OPENCLOUD_URL` / … vars the Rust portal reads — the landing page
fetches them server-side (`src/lib/config.ts`). ⚠️ Env reads use dynamic
property access (`serverEnv`) on purpose: static `process.env.X` is inlined at
build time by Next.js.

## Deployment

Two paths exist; the LIVE one (verified 2026-10-31) is a **host process**, not
a container:

```bash
# live: build standalone on the workstation, transfer to the 1blu host
npm run build                                      # .next/standalone + .next/static
rsync -az .next/standalone/ root@HOST:/opt/opensme-portal-web/
rsync -az .next/static/     root@HOST:/opt/opensme-portal-web/.next/static/
# on the host: restart the Next process (the watchdog relaunches it too)
#   setsid nohup env PORT=3001 HOSTNAME=127.0.0.1 \
#     BACKEND_URL="http://127.0.0.1:8087" NODE_ENV=production \
#     /opt/node-host/bin/node /opt/opensme-portal-web/server.js &>> /var/log/opensme-portal-web.log
```

Live wiring (2026-10-31): Traefik file-provider `portal-standin.yml` routes
`home.opensme.graphwiz.ai` → `127.0.0.1:3001` (this Next root at
`/opt/opensme-portal-web`, standalone `server.js`), which proxies `/api/*` to
the host Rust portal `127.0.0.1:8087` (`/opt/opensme-portal-rust/run.sh` +
`env.list` — owns login/session/logout). `/opt/opensme-host-services-watch.sh`
relaunches both. The compose `portal-web` container (profile `web`) stays
wedge-blocked; the compose `portal` container on `:8080` is a separate instance.

Container path (documented alt, only when the host process is superseded):

```bash
docker build -t compose-portal-web:latest -f portal/web/Dockerfile portal/web
docker save compose-portal-web:latest | gzip > /tmp/pweb.tar.gz
# copy + `docker load` on the host; the ansible role brings up the service via
# the `web` compose profile and points the portal file-provider stand-in at
# opensme-portal-web:3000.
```
