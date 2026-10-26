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

The image is built on the workstation and transferred (the 1blu host cannot
reach npm reliably), same as the Rust portal:

```bash
docker build -t compose-portal-web:latest -f portal/web/Dockerfile portal/web
docker save compose-portal-web:latest | gzip > /tmp/pweb.tar.gz
# copy + `docker load` on the host; the ansible role brings up the service via
# the `web` compose profile and points the portal file-provider stand-in at
# opensme-portal-web:3000.
```
