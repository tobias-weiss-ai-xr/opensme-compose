import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { backendUrl, forwardToBackend } from "@/lib/backend";
import { probeSession } from "@/lib/session";

/**
 * Proxy /metrics to the Rust backend so Prometheus keeps scraping the same
 * opensme_portal_* series at the same URL (runtime BACKEND_URL).
 *
 * Metrics are INTERNAL telemetry — never expose them on the public edge
 * unauthenticated: the histogram route labels enumerate the API surface and
 * sso/active-session counters leak usage. Anonymous callers get a 404 (no
 * existence leak). Real Prometheus deployments should scrape the Rust
 * backend on the internal network (BACKEND_URL) instead of this public URL;
 * if edge scraping is required, it needs a signed session cookie.
 */
export const dynamic = "force-dynamic";

export async function GET() {
  const authenticated = await probeSession(
    backendUrl(),
    (await cookies()).toString(),
  );
  if (!authenticated) {
    return new NextResponse("not found", { status: 404 });
  }
  try {
    const result = await forwardToBackend({
      url: `${backendUrl()}/metrics`,
      method: "GET",
      timeoutMs: 10_000,
    });
    if (!result.headers.get("content-type")) {
      result.headers.set("content-type", "text/plain; version=0.0.4");
    }
    return new NextResponse(Buffer.from(result.bytes), {
      status: result.status,
      headers: result.headers,
    });
  } catch {
    return new NextResponse("portal backend unreachable", { status: 502 });
  }
}
