import { NextRequest, NextResponse } from "next/server";
import { backendUrl, forwardToBackend } from "@/lib/backend";

/**
 * Same-origin proxy to the Rust axum API (runtime BACKEND_URL — dynamic
 * access defeats the build-time inliner). Every /api/* request is forwarded
 * verbatim (method, query, headers, raw body) and the upstream
 * status/body/content-type are echoed, so the browser keeps CSP
 * connect-src 'self' and the external API contract is unchanged.
 */

export const dynamic = "force-dynamic";

// Headers we forward to the backend. Cookie is deliberately included so
// HttpOnly-session-dependent endpoints (e.g. logout POST) keep working.
const FORWARD = new Set([
  "content-type",
  "accept",
  "accept-language",
  "authorization",
  "cookie",
  "user-agent",
  "x-requested-with",
]);

async function proxy(req: NextRequest, ctx: { params: Promise<{ path?: string[] }> }) {
  const { path } = await ctx.params;
  const suffix = (path ?? []).join("/");
  const qs = req.nextUrl.search;
  const url = `${backendUrl()}/api/${suffix}${qs}`;

  const headers = new Headers();
  req.headers.forEach((value, key) => {
    if (FORWARD.has(key.toLowerCase())) headers.set(key, value);
  });

  const body = ["GET", "HEAD"].includes(req.method)
    ? undefined
    : await req.arrayBuffer();

  try {
    const result = await forwardToBackend({
      url,
      method: req.method,
      headers,
      body: body as BodyInit | undefined,
      // matches the Rust portal's own timeout conventions; AI chat can be slow
      timeoutMs: 300_000,
    });
    if (req.method === "HEAD") {
      return new NextResponse(null, { status: result.status, headers: result.headers });
    }
    return new NextResponse(Buffer.from(result.bytes), {
      status: result.status,
      headers: result.headers,
    });
  } catch {
    return NextResponse.json(
      { error: "portal backend unreachable" },
      { status: 502 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
export const OPTIONS = proxy;
export const PATCH = proxy;
export const HEAD = proxy;
