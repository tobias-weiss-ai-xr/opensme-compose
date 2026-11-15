import { NextResponse } from "next/server";
import { aiEnabled, runAiHelper } from "@/lib/llm";

/**
 * POST /api/ai/summarize — LLM summary of an intercom thread (T6).
 *
 * Dedicated route, takes precedence over the [[...path]] catch-all.
 * Pipeline (lib/llm.ts): AI gate → {threadId} validation →
 * intercom thread fetch with the caller's session → transcript →
 * LLM chat (LiteLLM gateway first, ollama fallback) → 200 {summary}.
 *
 * AI is an operator opt-in: with neither OPENAI_API_KEY nor OLLAMA_URL
 * set every request (probe
 * GET included) is answered 503 {enabled:false} — the IntercomPanel
 * probes this and hides the 'Zusammenfassen' button, no error spam.
 */

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Probe — the UI's enablement check. */
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
  const result = await runAiHelper("summary", {
    body,
    cookie: req.headers.get("cookie"),
  });
  return NextResponse.json(result.payload, { status: result.status });
}
