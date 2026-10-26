import { NextRequest, NextResponse } from "next/server";
import { backendUrl, forwardToBackend } from "@/lib/backend";

/**
 * Proxy /metrics to the Rust backend so Prometheus keeps scraping the same
 * opensme_portal_* series at the same URL (runtime BACKEND_URL).
 */
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  void req;
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
