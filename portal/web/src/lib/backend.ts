/**
 * Runtime backend wiring. Dynamic property access defeats the Next.js
 * build-time env inliner — the value is resolved per request in the running
 * container, not frozen during `next build`.
 */

export function backendUrl(): string {
  return process.env.BACKEND_URL || "http://localhost:8080";
}

export interface ForwardTarget {
  url: string;
  method: string;
  headers?: HeadersInit;
  body?: BodyInit | undefined;
  timeoutMs?: number;
}

export interface ForwardResult {
  status: number;
  headers: Headers;
  bytes: ArrayBuffer;
}

/** Headers copied from the upstream response to the portal caller. */
const ECHO_HEADERS = [
  "content-type",
  "content-disposition",
  "content-length",
  // Logout contract: POST /api/logout answers 303 + Location (IdP
  // end-session) and clears the HttpOnly session cookie via Set-Cookie.
  "location",
  "set-cookie",
];

/**
 * Forward one request to the Rust backend and echo status/body/content-type
 * verbatim. Pure fetch (no Next types) so it is directly unit-testable.
 */
export async function forwardToBackend(target: ForwardTarget): Promise<ForwardResult> {
  const upstream = await fetch(target.url, {
    method: target.method,
    headers: target.headers,
    body: target.body,
    // The logout contract must hand the 303 + Location straight to the
    // browser — never follow/consume the redirect server-side.
    redirect: "manual",
    signal: AbortSignal.timeout(target.timeoutMs ?? 10_000),
    cache: "no-store",
  });
  const headers = new Headers();
  ECHO_HEADERS.forEach((name) => {
    if (name === "set-cookie") {
      const cookies =
        typeof upstream.headers.getSetCookie === "function"
          ? upstream.headers.getSetCookie()
          : (() => {
              const v = upstream.headers.get("set-cookie");
              return v ? [v] : [];
            })();
      cookies.forEach((c) => headers.append("set-cookie", c));
      return;
    }
    const v = upstream.headers.get(name);
    if (v) headers.set(name, v);
  });
  headers.set("cache-control", "no-store");
  return {
    status: upstream.status,
    headers,
    bytes: await upstream.arrayBuffer(),
  };
}
