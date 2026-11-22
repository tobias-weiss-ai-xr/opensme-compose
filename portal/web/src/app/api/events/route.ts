import { NextRequest } from "next/server";
import { backendUrl } from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * Same-origin SSE proxy. The catch-all /api/[[...path]] route buffers whole
 * response bodies and MUST NOT be used for text/event-stream. This route
 * forwards only the headers needed for session auth and returns the upstream
 * ReadableStream unchanged, so EventSource keeps CSP connect-src 'self'.
 */
export async function GET(req: NextRequest) {
  const headers = new Headers();
  for (const key of ["cookie", "user-agent", "last-event-id"]) {
    const value = req.headers.get(key);
    if (value) headers.set(key, value);
  }
  headers.set("accept", "text/event-stream");

  let upstream: Response;
  try {
    upstream = await fetch(`${backendUrl()}/api/events`, {
      headers,
      cache: "no-store",
      signal: req.signal,
    });
  } catch {
    return new Response(null, { status: 502 });
  }
  if (!upstream.ok || !upstream.body) {
    return new Response(null, { status: upstream.status === 401 ? 401 : 204 });
  }
  return new Response(upstream.body, {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    },
  });
}
