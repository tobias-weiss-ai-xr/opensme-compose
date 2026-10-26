import { NextRequest, NextResponse } from "next/server";
import { backendUrl, forwardToBackend } from "@/lib/backend";

/** Proxy /health to the Rust backend (runtime BACKEND_URL). */
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  void req;
  try {
    const result = await forwardToBackend({
      url: `${backendUrl()}/health`,
      method: "GET",
      timeoutMs: 10_000,
    });
    return new NextResponse(Buffer.from(result.bytes), {
      status: result.status,
      headers: result.headers,
    });
  } catch {
    return new NextResponse("portal backend unreachable", { status: 502 });
  }
}
