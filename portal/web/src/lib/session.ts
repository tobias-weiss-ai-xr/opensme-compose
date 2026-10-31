/**
 * Server-side session probe — the portal's public `GET /api/session`
 * (Rust axum, session.rs) answers 200 `{authenticated:boolean}`. It is
 * probed during SSR (page.tsx) with the request's cookies forwarded, so
 * anonymous visitors never receive the Dienste tile grid in the HTML
 * (and signed-in visitors never flash a login prompt). The probe is
 * fail-closed: any non-200, malformed or failed request means
 * "not authenticated" → tiles stay hidden.
 */

export interface SessionProbeOptions {
  /** Injectable fetch (tests). */
  fetchImpl?: typeof fetch;
  /** Abort timeout in ms (default 5000). */
  timeoutMs?: number;
}

export async function probeSession(
  base: string,
  cookieHeader: string,
  options: SessionProbeOptions = {},
): Promise<boolean> {
  const { fetchImpl = fetch, timeoutMs = 5000 } = options;
  try {
    const res = await fetchImpl(`${base.replace(/\/+$/, "")}/api/session`, {
      // Replicate the browser: the same-origin proxy forwards the request
      // cookies to the backend, so does the SSR probe.
      headers: cookieHeader.trim() ? { cookie: cookieHeader } : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { authenticated?: boolean };
    return data.authenticated === true;
  } catch {
    return false;
  }
}
