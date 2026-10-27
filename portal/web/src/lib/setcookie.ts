/**
 * Set-Cookie pass-through helpers for the /api/* Same-origin proxy
 * (src/app/api/[[...path]]/route.ts). Next's runtime does not reliably
 * carry raw `set-cookie` headers from a constructed NextResponse, so the
 * proxy re-applies each upstream cookie via the ResponseCookies API.
 */

/** All `Set-Cookie` header values (undici Headers expose getSetCookie). */
export function getSetCookieList(h: Headers): string[] {
  if (typeof (h as Headers & { getSetCookie?: () => string[] }).getSetCookie === "function") {
    return (h as Headers & { getSetCookie: () => string[] }).getSetCookie();
  }
  // Fallback: undici also offers a comma-joined `set-cookie`. Splitting on
  // ", " is safe for the portal's cookies (they use Max-Age, never Expires,
  // so no embedded commas).
  const v = h.get("set-cookie");
  return v ? v.split(/,\s*/) : [];
}

/** Minimal RFC 6265 split — preserves Path/HttpOnly/SameSite/Secure/Max-Age. */
export function parseSetCookie(raw: string): {
  name: string;
  value: string;
  path?: string;
  httpOnly?: boolean;
  secure?: boolean;
  sameSite?: "lax" | "strict" | "none";
  maxAge?: number;
} {
  const [nameValue, ...parts] = raw.split(";").map((s) => s.trim());
  const eq = nameValue.indexOf("=");
  const name = nameValue.slice(0, eq).trim();
  const value = nameValue.slice(eq + 1).trim();
  const opts: Record<string, string> = {};
  for (const p of parts) {
    const [k, ...rest] = p.split("=");
    opts[k.trim().toLowerCase()] = rest.join("=").trim();
  }
  return {
    name,
    value,
    path: opts.path || "/",
    httpOnly: "httponly" in opts,
    secure: "secure" in opts,
    sameSite: (opts.samesite ?? "").toLowerCase() as
      | "lax"
      | "strict"
      | "none"
      | undefined,
    ...(opts["max-age"] !== undefined ? { maxAge: Number(opts["max-age"]) } : {}),
  };
}
