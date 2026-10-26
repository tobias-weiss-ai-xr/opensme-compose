import { NextResponse } from "next/server";

/**
 * Nonce-based CSP. Next.js's App Router flushes inline bootstrap scripts,
 * so a strict `script-src 'self'` alone would break hydration. Instead each
 * HTML response gets a per-request nonce: Next reads the `nonce-…` token
 * from the CSP header and tags its inline scripts with the same nonce,
 * while `'unsafe-inline'` stays out of script-src — untrusted feed /
 * intercom / poll text rendered into the DOM cannot execute inline handlers.
 */

export function middleware() {
  const nonce = Buffer.from(crypto.randomUUID().replace(/-/g, "")).toString(
    "base64",
  );

  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}'`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");

  const response = NextResponse.next();
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  // everything except internal assets served from _next
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
