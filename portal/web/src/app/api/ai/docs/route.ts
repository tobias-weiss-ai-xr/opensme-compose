import { NextResponse } from "next/server";
import { aiEnabled } from "@/lib/llm";
import { runDocAnalysis } from "@/lib/ai-docs";

/**
 * POST /api/ai/docs — KI-Analyse of an OpenCloud file (T2).
 *
 * Dedicated route, takes precedence over the [[...path]] catch-all.
 * Pipeline (lib/ai-docs.ts): OLLAMA_URL gate → {path, question?} →
 * WebDAV download via lib/clouddav.ts (same env resolution/auth as the
 * cloud listing) → 422 {error:'Dateityp nicht unterstützbar'} when the
 * file has no extractable text → German instruction → Ollama
 * /api/chat → 200 {answer}.
 *
 * AI is an operator opt-in: with OLLAMA_URL unset every request (probe
 * GET included) is answered 503 {enabled:false}.
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
  const body = (await req.json().catch(() => null)) as {
    path?: unknown;
    question?: unknown;
  } | null;
  const result = await runDocAnalysis({ body });
  return NextResponse.json(result.payload, { status: result.status });
}
