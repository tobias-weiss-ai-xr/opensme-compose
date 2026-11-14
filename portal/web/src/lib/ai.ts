/**
 * Same-origin client for the AI helper routes (/api/ai/summarize,
 * /api/ai/draft). Lives beside lib/api.ts but in its own module so the
 * AI surfaces stay opt-in without touching the existing API client
 * (the IntercomPanel suite mocks @/lib/api wholesale — a probe here must
 * never break those tests).
 *
 * The mount-time probe is deliberately swallow-everything: any failure
 * (offline, 404, 503, backend down) just means "AI disabled" — the UI
 * hides the buttons, no error spam, no unhandled rejections.
 */
import { AuthError } from "@/lib/api";

/**
 * Probe the AI gate via GET /api/ai/summarize: {enabled:true} when the
 * operator configured OLLAMA_URL, 503 {enabled:false} otherwise. Resolves
 * false on ANY failure — never throws.
 */
export async function aiEnabledProbe(): Promise<boolean> {
  try {
    const res = await fetch("/api/ai/summarize", { cache: "no-store" });
    if (!res.ok) return false;
    const data = (await res.json()) as { enabled?: boolean };
    return data?.enabled === true;
  } catch {
    return false; // disabled / unreachable — silence is the contract
  }
}

async function postAi<T>(path: string, threadId: number): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ threadId }),
  });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) {
    let error = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data?.error) error = data.error;
    } catch {
      /* keep HTTP fallback */
    }
    throw new Error(error);
  }
  return res.json() as Promise<T>;
}

/** Ask the LLM for a summary of the thread around note `threadId`. */
export function summarizeThread(threadId: number): Promise<{ summary: string }> {
  return postAi("/api/ai/summarize", threadId);
}

/** Ask the LLM for a draft reply to note `threadId`. */
export function draftReply(threadId: number): Promise<{ draft: string }> {
  return postAi("/api/ai/draft", threadId);
}
