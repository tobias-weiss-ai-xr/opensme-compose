import { NextResponse } from "next/server";
import { aiEnabled, runAiHelper } from "@/lib/llm";

/**
 * POST /api/ai/draft — LLM draft-reply suggestion for an intercom note
 * (T6). Same contract as /api/ai/summarize: AI gate — OPENAI_API_KEY or
 * OLLAMA_URL configured, else 503 {enabled:false} which hides the
 * surface → {threadId} → thread fetch with the caller's session →
 * transcript (target note marked) → LLM chat (gateway first, ollama
 * fallback) → 200 {draft}. Shared pipeline lives in lib/llm.ts.
 */

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Probe — same gate semantics as the summarize route. */
export function GET(): Response {
  if (!aiEnabled()) {
    return NextResponse.json({ enabled: false }, { status: 503 });
  }
  return NextResponse.json({ enabled: true });
}

export async function POST(req: Request): Promise<Response> {
  if (!aiEnabled()) {
    return NextResponse.json({ enabled: false }, { status: 503 });
  }
  const body = (await req.json().catch(() => null)) as { threadId?: unknown } | null;
  const result = await runAiHelper("draft", {
    body,
    cookie: req.headers.get("cookie"),
  });
  return NextResponse.json(result.payload, { status: result.status });
}
