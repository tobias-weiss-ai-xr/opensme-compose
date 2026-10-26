import type { NextConfig } from "next";

/**
 * openSME portal — Next.js UI in front of the Rust axum API.
 *
 * The Rust service (`portal`) owns the security-sensitive + e2e-tested
 * surface: /api/* (services, announcements, feeds, intercom, polls, AI,
 * logout), /health and /metrics (Prometheus). This app renders the landing
 * page and reaches the backend same-origin — every /api|/health|/metrics
 * request is proxied by route handlers (src/app/api/[[...path]]/route.ts,
 * src/app/health, src/app/metrics) that read BACKEND_URL at RUNTIME, so
 * the browser never leaves this origin (CSP connect-src 'self' holds) and
 * the external contract is unchanged.
 *
 * BACKEND_URL: http://portal:8080 in compose, http://localhost:8080 in dev.
 */
export const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "frame-src 'none'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
];

// NOTE: the Content-Security-Policy header itself is NOT set here — the
// portal needs a per-request nonce for Next's inline bootstrap scripts, so
// src/middleware.ts owns the CSP. Remaining static security headers live
// below.
const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains; preload" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  // NOTE: do NOT read process.env here for runtime configuration — Next.js
  // inlines env reads in next.config at BUILD time. The same-origin API
  // surface is instead proxied by route handlers (src/app/api/[[...path]])
  // which read BACKEND_URL per request. The portal must also never be
  // statically pre-rendered.
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
