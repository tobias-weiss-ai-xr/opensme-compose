/**
 * Nonce-based Content-Security-Policy builder for the portal frontend.
 *
 * The middleware (src/middleware.ts) owns the actual header — this module
 * keeps the policy construction pure so it is unit-testable and the
 * cross-origin logout hop cannot silently regress.
 *
 * Why `form-action` needs the IdP origin: the logout button is a native
 * form POST /api/logout (no JS — R17 kept it JS-free for the
 * security-critical action). The Rust handler answers 303 to the IdP
 * end-session URL, i.e. the form submission navigates CROSS-ORIGIN.
 * Chrome enforces `form-action` on redirects of form submissions, so
 * `form-action 'self'` alone blocks the follow and the user silently
 * stays logged in at the IdP (found live 2026-11-19 while verifying the
 * R20 gallery: Abmelden fired the POST, got the 303, but the browser
 * aborted the redirect with a misleading "form-action 'self'" console
 * message naming the POST target). The trusted IdP origin is therefore
 * allow-listed alongside 'self'.
 */

/** Reduce an IdP URL (or bare domain) to its origin, or "" when unset. */
export function idpOriginFromUrl(input?: string): string {
  const raw = input?.trim() ?? "";
  if (!raw) return "";
  try {
    return new URL(raw).origin;
  } catch {
    try {
      return new URL(`https://${raw}`).origin;
    } catch {
      return "";
    }
  }
}

/** Build the strict nonce-based CSP; form-action allows 'self' plus the
 *  IdP origin for the logout 303 hop. */
export function buildCsp(nonce: string, idpOrigin = ""): string {
  const formAction = idpOrigin
    ? `form-action 'self' ${idpOrigin}`
    : "form-action 'self'";
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}'`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    formAction,
  ].join("; ");
}
