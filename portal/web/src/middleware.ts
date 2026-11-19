import { NextResponse, type NextRequest } from "next/server";
import { buildCsp, idpOriginFromUrl } from "./lib/csp";

/**
 * Nonce-based CSP. Next.js's App Router flushes inline bootstrap scripts,
 * so a strict `script-src 'self'` alone would break hydration. Instead each
 * HTML response gets a per-request nonce: Next reads the `nonce-…` token
 * from the CSP header and tags its inline scripts with the same nonce,
 * while `'unsafe-inline'` stays out of script-src — untrusted feed /
 * intercom / poll text rendered into the DOM cannot execute inline handlers.
 *
 * The nonce has to reach the renderer TWICE:
 *  - through the REQUEST headers (app-render's parseRequestHeaders only
 *    sees req.headers — a response-only CSP never tags the inline scripts,
 *    hydration stays blank; found/enforced 2026-11-19 on /logged-out);
 *  - through the RESPONSE headers, so the browser actually enforces it.
 *
 * IDP_URL is allow-listed in `form-action` (see lib/csp.ts): the logout
 * form POST /api/logout 303s to the IdP end-session endpoint — a
 * cross-origin hop Chrome enforces against `form-action`.
 */

export function middleware(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID().replace(/-/g, "")).toString(
    "base64",
  );
  const csp = buildCsp(nonce, idpOriginFromUrl(process.env.IDP_URL));

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set(
    "Content-Security-Policy",
    csp,
  );
  return response;
}

export const config = {
  // everything except internal assets served from _next
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
