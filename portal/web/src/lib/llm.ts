/**
 * Server-side LLM abstraction for the AI helper surfaces (thread summary,
 * draft-reply suggestion) — OpenAI-compatible gateway first, Ollama as
 * the fallback, chat-API style on both legs.
 *
 * Gateway leg (preferred): openaiChat() POSTs
 * {OPENAI_BASE_URL:-http://litellm:4000/v1}/chat/completions with
 * Authorization: Bearer OPENAI_API_KEY and { model: openaiModel()
 * (OPENAI_MODEL, default "glm-5.3-flash"), messages, stream:false }.
 * The LiteLLM gateway answers in seconds — non-streaming is fine, well
 * under undici's 300 s headersTimeout.
 *
 * Ollama leg (fallback): ollamaChat() POSTs
 * {OLLAMA_URL:-http://ai:11434}/api/chat with { model, messages,
 * stream:true } — STREAMING (NDJSON) so slow CPU inference never trips
 * undici's 300 s headersTimeout. The model comes from AI_MODEL
 * (OLLAMA_MODEL as the analysis-stack fallback, "llama3.1" as last
 * resort).
 *
 * The dispatch (aiChat) prefers the gateway when OPENAI_API_KEY is set
 * (aiGatewayEnabled()) and silently degrades to ollamaChat when the
 * gateway is unset or its call fails.
 *
 * AI is an operator opt-in: with neither OPENAI_API_KEY nor OLLAMA_URL
 * set the surfaces stay disabled (aiEnabled() === false → routes answer
 * 503 {enabled:false}, the UI hides the buttons). No live LLM exists on
 * every host — every failure path (unreachable, timeout, non-JSON, empty
 * answer) resolves into a graceful LlmError that the routes surface as
 * 502 with a plain German message instead of leaking stack traces or
 * URLs.
 *
 * Framework-free (no next/* imports) so the whole module is directly
 * unit-testable with a stubbed global fetch (llm.test.ts).
 */
import { serverEnv } from "@/lib/config";
import { backendUrl, forwardToBackend } from "@/lib/backend";
import type { IntercomMessage } from "@/lib/types";

/** One chat turn for the Ollama /api/chat body. */
export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

/**
 * Graceful AI failure — routes map this to 502 {error}. The message is
 * user-facing (German, no URLs/secrets); `detail` keeps the technical
 * cause for the server log only.
 */
export class LlmError extends Error {
  readonly detail?: unknown;
  constructor(message: string, detail?: unknown) {
    super(message);
    this.name = "LlmError";
    this.detail = detail;
  }
}

/** Raised when fetching the intercom thread server-side fails. */
export class ThreadFetchError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ThreadFetchError";
    this.status = status;
  }
}

/**
 * True when the operator configured any LLM backend — the OpenAI-compatible
 * gateway (OPENAI_API_KEY) or an Ollama endpoint (OLLAMA_URL). Route gate:
 * false → 503 {enabled:false}.
 */
export function aiEnabled(): boolean {
  return (
    serverEnv("OPENAI_API_KEY").trim() !== "" || serverEnv("OLLAMA_URL").trim() !== ""
  );
}

/** Chat endpoint base — spec default http://ai:11434 (compose `ai` service). */
export function ollamaUrl(): string {
  return serverEnv("OLLAMA_URL", "http://ai:11434").trim().replace(/\/+$/, "");
}

/** Model name for /api/chat — AI_MODEL, OLLAMA_MODEL fallback, llama3.1. */
export function aiModel(): string {
  return serverEnv("AI_MODEL") || serverEnv("OLLAMA_MODEL") || "llama3.1";
}

/**
 * True when the operator configured the OpenAI-compatible gateway
 * (LiteLLM) — the fast leg the dispatch prefers.
 */
export function aiGatewayEnabled(): boolean {
  return serverEnv("OPENAI_API_KEY").trim() !== "";
}

/** Gateway chat endpoint base — LiteLLM default http://litellm:4000/v1. */
export function openaiBaseUrl(): string {
  return serverEnv("OPENAI_BASE_URL", "http://litellm:4000/v1")
    .trim()
    .replace(/\/+$/, "");
}

/** Model name for /v1/chat/completions — OPENAI_MODEL, glm-5.3-flash. */
export function openaiModel(): string {
  return serverEnv("OPENAI_MODEL") || "glm-5.3-flash";
}

/** Per-request budget for one LLM call (summaries/drafts can be slow). */
export function aiTimeoutMs(): number {
  const parsed = Number(serverEnv("AI_TIMEOUT_MS"));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 120_000;
}

export interface OllamaChatOptions {
  /** Endpoint override — tests only; production uses ollamaUrl(). */
  url?: string;
  /** Fetch override — tests only; production uses global fetch. */
  fetchImpl?: typeof fetch;
}

/**
 * One Ollama chat roundtrip, resolving with the assistant message content.
 * The call is STREAMING (stream:true, NDJSON chat chunks): response headers
 * arrive immediately, so slow CPU inference never trips undici's default
 * 300 s headersTimeout — only the overall AbortSignal budget applies. A
 * single JSON object body (stream:false backends / test mocks) parses the
 * same way. Every failure mode is normalized into LlmError (timeout,
 * unreachable, HTTP error, non-JSON body, empty answer) — callers only
 * ever see the graceful message.
 */
export async function ollamaChat(
  messages: ChatMessage[],
  options: OllamaChatOptions = {},
): Promise<string> {
  const url = `${options.url ?? ollamaUrl()}/api/chat`;
  const doFetch = options.fetchImpl ?? fetch;
  const timeoutMs = aiTimeoutMs();
  let res: Response;
  try {
    res = await doFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: aiModel(), messages, stream: true }),
      signal: AbortSignal.timeout(timeoutMs),
      cache: "no-store",
    });
  } catch (err) {
    // DOMException is not `instanceof Error` in some runtimes — read the
    // name structurally so timeout aborts are recognized everywhere.
    const name = (err as { name?: unknown } | null)?.name;
    if (name === "TimeoutError" || name === "AbortError") {
      throw new LlmError(
        `KI nicht erreichbar (Timeout nach ${Math.round(timeoutMs / 1000)} s)`,
        err,
      );
    }
    throw new LlmError("KI nicht erreichbar", err);
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = (await res.json()) as { error?: unknown };
      if (typeof data?.error === "string" && data.error) detail = data.error;
    } catch {
      /* keep the HTTP fallback */
    }
    throw new LlmError(`KI-Antwort fehlgeschlagen (${detail})`);
  }
  const raw = await res.text();
  let content = "";
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let obj: { message?: { content?: unknown } };
    try {
      obj = JSON.parse(trimmed) as { message?: { content?: unknown } };
    } catch {
      throw new LlmError("KI-Antwort nicht verständlich (kein JSON)");
    }
    const piece = obj?.message?.content;
    if (typeof piece === "string") content += piece;
  }
  if (!content.trim()) {
    throw new LlmError("KI lieferte eine leere Antwort");
  }
  return content.trim();
}

export interface OpenaiChatOptions {
  /** Endpoint override — tests only; production uses openaiBaseUrl(). */
  url?: string;
  /** Fetch override — tests only; production uses global fetch. */
  fetchImpl?: typeof fetch;
}

/**
 * One OpenAI-compatible chat roundtrip (LiteLLM gateway), resolving with
 * the assistant message content. Non-streaming (stream:false): the
 * gateway answers in seconds, so response headers arrive well under
 * undici's 300 s headersTimeout — only the overall AbortSignal budget
 * applies. Every failure mode is normalized into LlmError exactly like
 * ollamaChat (timeout, unreachable, HTTP error, non-JSON body, empty
 * answer) — callers only ever see the graceful message.
 */
export async function openaiChat(
  messages: ChatMessage[],
  options: OpenaiChatOptions = {},
): Promise<string> {
  const url = `${options.url ?? openaiBaseUrl()}/chat/completions`;
  const doFetch = options.fetchImpl ?? fetch;
  const timeoutMs = aiTimeoutMs();
  let res: Response;
  try {
    res = await doFetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${serverEnv("OPENAI_API_KEY")}`,
      },
      body: JSON.stringify({ model: openaiModel(), messages, stream: false }),
      signal: AbortSignal.timeout(timeoutMs),
      cache: "no-store",
    });
  } catch (err) {
    // Same structural name check as ollamaChat — DOMException is not
    // `instanceof Error` in some runtimes, so timeout aborts are
    // recognized everywhere.
    const name = (err as { name?: unknown } | null)?.name;
    if (name === "TimeoutError" || name === "AbortError") {
      throw new LlmError(
        `KI nicht erreichbar (Timeout nach ${Math.round(timeoutMs / 1000)} s)`,
        err,
      );
    }
    throw new LlmError("KI nicht erreichbar", err);
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = (await res.json()) as { error?: unknown };
      const msg = data?.error;
      if (typeof msg === "string" && msg) {
        detail = msg;
      } else if (
        msg &&
        typeof msg === "object" &&
        typeof (msg as { message?: unknown }).message === "string" &&
        (msg as { message?: unknown }).message
      ) {
        // OpenAI-style error shape: { error: { message: "..." } }
        detail = (msg as { message: string }).message;
      }
    } catch {
      /* keep the HTTP fallback */
    }
    throw new LlmError(`KI-Antwort fehlgeschlagen (${detail})`);
  }
  const raw = await res.text();
  let data: { choices?: Array<{ message?: { content?: unknown } }> };
  try {
    data = JSON.parse(raw) as { choices?: Array<{ message?: { content?: unknown } }> };
  } catch {
    throw new LlmError("KI-Antwort nicht verständlich (kein JSON)");
  }
  const content = data?.choices?.[0]?.message?.content;
  if (typeof content !== "string" || !content.trim()) {
    throw new LlmError("KI lieferte eine leere Antwort");
  }
  return content.trim();
}

/**
 * Chat dispatch for the helper pipeline — gateway first, ollama fallback:
 * uses openaiChat() when the gateway is configured (aiGatewayEnabled());
 * on an unset gateway OR a failed gateway call it degrades to ollamaChat()
 * (dialled only when OLLAMA_URL is actually configured — never a default
 * endpoint the operator never set). Log-less and error-tolerant: a
 * gateway outage silently degrades to ollama; when both legs fail the
 * last LlmError propagates and runAiHelper maps it to 502.
 */
export async function aiChat(messages: ChatMessage[]): Promise<string> {
  if (aiGatewayEnabled()) {
    try {
      return await openaiChat(messages);
    } catch (err) {
      if (serverEnv("OLLAMA_URL").trim() === "") throw err;
      return ollamaChat(messages);
    }
  }
  return ollamaChat(messages);
}

/** Thread transcript budget — enough context, still a sane prompt size. */
export const THREAD_TRANSCRIPT_CAP = 40;

/**
 * Fetch the intercom thread server-side with the caller's session cookie
 * (same hop the client's fetchIntercom takes, but via forwardToBackend —
 * the broadcast route's pattern). The Rust store has no single-note GET,
 * so the full list is fetched and the caller filters by id.
 */
export async function fetchIntercomThread(cookie: string | null): Promise<IntercomMessage[]> {
  const headers = new Headers();
  if (cookie) headers.set("cookie", cookie);
  let upstream: Awaited<ReturnType<typeof forwardToBackend>>;
  try {
    upstream = await forwardToBackend({
      url: `${backendUrl()}/api/intercom`,
      method: "GET",
      headers,
      timeoutMs: 10_000,
    });
  } catch {
    throw new ThreadFetchError("portal backend unreachable", 502);
  }
  if (upstream.status === 401) {
    throw new ThreadFetchError("unauthorized", 401);
  }
  if (upstream.status < 200 || upstream.status >= 300) {
    throw new ThreadFetchError(`HTTP ${upstream.status}`, upstream.status);
  }
  try {
    const data = JSON.parse(new TextDecoder().decode(upstream.bytes)) as {
      messages?: IntercomMessage[];
    };
    if (!Array.isArray(data?.messages)) throw new Error("no messages");
    return data.messages;
  } catch {
    throw new ThreadFetchError("intercom list not readable", 502);
  }
}

/**
 * Render the thread notes as a numbered transcript for the prompt. The
 * target note (the one being summarized / replied to) is marked so the
 * model knows where to anchor. Newest first — matching the API order.
 */
export function buildTranscript(
  messages: IntercomMessage[],
  targetId: number,
  cap: number = THREAD_TRANSCRIPT_CAP,
): string {
  return messages
    .slice(0, cap)
    .map((m) => {
      const author = m.author?.trim() || "unbekannt";
      const text = m.text?.trim() || "(leer)";
      const marker = m.id === targetId ? "  ← Antwort-Ziel" : "";
      return `[#${m.id}] ${author}: ${text}${marker}`;
    })
    .join("\n");
}

/** Prompt: compact German summary of the whole thread. */
export function summarizeMessages(transcript: string): ChatMessage[] {
  return [
    {
      role: "system",
      content:
        "Du bist ein hilfreicher Assistent in einem internen Team-Notiz-Tool. " +
        "Fasse den Thread in höchstens 4 kurzen Sätzen auf Deutsch zusammen. " +
        "Antworte NUR mit dem Zusammenfassungstext — ohne Einleitung, ohne Meta.",
    },
    {
      role: "user",
      content: `Fasse diesen Team-Notizen-Thread zusammen:\n\n${transcript}`,
    },
  ];
}

/** Prompt: a short, ready-to-post reply suggestion for the target note. */
export function draftReplyMessages(transcript: string): ChatMessage[] {
  return [
    {
      role: "system",
      content:
        "Du formulierst kurze, professionelle Antworten (höchstens 3 Sätze, Deutsch) " +
        "für ein internes Team-Notiz-Tool. Antworte NUR mit dem Antworttext — " +
        "keine Einleitung, keine Erklärungen.",
    },
    {
      role: "user",
      content:
        `Schlage eine Antwort auf die mit „← Antwort-Ziel" markierte Notiz vor. ` +
        `Berücksichtige den restlichen Thread als Kontext:\n\n${transcript}`,
    },
  ];
}

export type AiHelperKind = "summary" | "draft";

export interface AiHelperResult {
  status: number;
  payload: Record<string, unknown>;
}

export interface AiHelperRequest {
  /** Body of the incoming POST (already read). */
  body: { threadId?: unknown } | null;
  /** Caller's cookie header (session hop to the backend). */
  cookie: string | null;
  /** LLM call override — tests only. */
  chat?: (messages: ChatMessage[]) => Promise<string>;
}

/**
 * Shared summarize/draft pipeline (thin routes wrap the result in JSON):
 * thread fetch (caller session) → target lookup → transcript → prompt →
 * LLM → shaped payload. Every failure comes back as a plain
 * {status, payload:{error|enabled}} — never an exception.
 */
export async function runAiHelper(
  kind: AiHelperKind,
  input: AiHelperRequest,
): Promise<AiHelperResult> {
  const threadId =
    typeof input.body?.threadId === "number" &&
    Number.isInteger(input.body.threadId) &&
    input.body.threadId > 0
      ? input.body.threadId
      : null;
  if (threadId === null) {
    return { status: 400, payload: { error: "threadId fehlt oder ungültig" } };
  }

  let messages: IntercomMessage[];
  try {
    messages = await fetchIntercomThread(input.cookie);
  } catch (err) {
    if (err instanceof ThreadFetchError) {
      return { status: err.status, payload: { error: err.message } };
    }
    return { status: 502, payload: { error: "portal backend unreachable" } };
  }

  const target = messages.find((m) => m.id === threadId);
  if (!target) {
    return { status: 404, payload: { error: "Notiz nicht gefunden" } };
  }

  const transcript = buildTranscript(messages, threadId);
  const chat = input.chat ?? aiChat;
  try {
    const content =
      kind === "summary"
        ? await chat(summarizeMessages(transcript))
        : await chat(draftReplyMessages(transcript));
    return kind === "summary"
      ? { status: 200, payload: { summary: content } }
      : { status: 200, payload: { draft: content } };
  } catch (err) {
    const message = err instanceof LlmError ? err.message : "KI nicht erreichbar";
    return { status: 502, payload: { error: message } };
  }
}
