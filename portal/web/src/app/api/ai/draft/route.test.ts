import { GET, POST } from "./route";
import type { IntercomMessage } from "@/lib/types";

/**
 * Route tests for /api/ai/draft — same contract as summarize (gate,
 * validation, session hop, mocked LLM), leaner set since the shared
 * pipeline in lib/llm.ts is exhaustively covered there.
 */

function note(partial: Partial<IntercomMessage> & { id: number; text: string }): IntercomMessage {
  return { attachment: null, ...partial };
}

const NOTES: IntercomMessage[] = [
  note({ id: 4, text: "Braucht noch ein Review", author: "Anna" }),
];

function post(body: unknown): Request {
  return new Request("http://portal.local/api/ai/draft", {
    method: "POST",
    headers: { "Content-Type": "application/json", cookie: "OPENSMESTATE=t" },
    body: JSON.stringify(body),
  });
}

function urlRoutedFetch(handlers: Record<string, (init?: RequestInit) => Response>) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const handler = Object.entries(handlers).find(([suffix]) => url.endsWith(suffix));
    if (!handler) throw new Error(`unexpected fetch: ${url}`);
    return handler[1](init);
  });
}

describe("POST/GET /api/ai/draft", () => {
  beforeEach(() => {
    vi.stubEnv("OPENAI_API_KEY", "");
    vi.stubEnv("OLLAMA_URL", "http://ai:11434");
    vi.stubEnv("BACKEND_URL", "http://portal:8080");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("GET probe: 503 {enabled:false} without OLLAMA_URL, 200 {enabled:true} with", async () => {
    vi.stubEnv("OPENAI_API_KEY", "");
    vi.stubEnv("OLLAMA_URL", "");
    expect((await GET()).status).toBe(503);
    await expect((await GET()).json()).resolves.toEqual({ enabled: false });
    vi.stubEnv("OLLAMA_URL", "http://ai:11434");
    expect((await GET()).status).toBe(200);
    await expect((await GET()).json()).resolves.toEqual({ enabled: true });
  });

  it("GET probe: 200 {enabled:true} with only the gateway key, 503 when both unset", async () => {
    vi.stubEnv("OPENAI_API_KEY", "sk-litellm");
    vi.stubEnv("OLLAMA_URL", "");
    expect((await GET()).status).toBe(200);
    await expect((await GET()).json()).resolves.toEqual({ enabled: true });
    vi.stubEnv("OPENAI_API_KEY", "");
    expect((await GET()).status).toBe(503);
    await expect((await GET()).json()).resolves.toEqual({ enabled: false });
    expect((await POST(post({ threadId: 4 }))).status).toBe(503); // gate fires before the body
  });

  it("POST: 503 {enabled:false} when the AI gate is off", async () => {
    vi.stubEnv("OLLAMA_URL", "");
    expect((await POST(post({ threadId: 4 }))).status).toBe(503);
  });

  it("POST: happy path — 200 {draft} anchored on the target note", async () => {
    let chatBody: { messages: { content: string }[] } | undefined;
    vi.stubGlobal(
      "fetch",
      urlRoutedFetch({
        "/api/intercom": () =>
          new Response(JSON.stringify({ messages: NOTES, latest: 4 }), { status: 200 }),
        "/api/chat": (init) => {
          chatBody = JSON.parse(String(init?.body));
          return new Response(
            JSON.stringify({ message: { content: "Mache ich heute noch." } }),
            { status: 200 },
          );
        },
      }),
    );
    const res = await POST(post({ threadId: 4 }));
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ draft: "Mache ich heute noch." });
    expect(chatBody?.messages[1].content).toContain("Braucht noch ein Review");
    expect(chatBody?.messages[1].content).toContain("← Antwort-Ziel");
  });

  it("POST: 404 on unknown threadId, 502 on LLM failure", async () => {
    vi.stubGlobal(
      "fetch",
      urlRoutedFetch({
        "/api/intercom": () =>
          new Response(JSON.stringify({ messages: NOTES, latest: 4 }), { status: 200 }),
        "/api/chat": () => new Response(JSON.stringify({ error: "overloaded" }), { status: 500 }),
      }),
    );
    expect((await POST(post({ threadId: 99 }))).status).toBe(404);
    const res = await POST(post({ threadId: 4 }));
    expect(res.status).toBe(502);
    const data = (await res.json()) as { error?: string };
    expect(data.error).toContain("KI-Antwort fehlgeschlagen");
  });
});
