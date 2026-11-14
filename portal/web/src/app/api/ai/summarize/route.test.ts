import { GET, POST } from "./route";
import type { IntercomMessage } from "@/lib/types";

/**
 * Route tests for /api/ai/summarize: the OLLAMA_URL gate (503
 * {enabled:false} hides the surface, GET probe answers {enabled:true}),
 * validation, session hop to the intercom backend, and the mocked-LLM
 * happy path — all fetch via a URL-routed stub (no live AI).
 */

function note(partial: Partial<IntercomMessage> & { id: number; text: string }): IntercomMessage {
  return { attachment: null, ...partial };
}

const NOTES: IntercomMessage[] = [
  note({ id: 7, text: "Deploy steht", author: "Bob" }),
  note({ id: 6, text: "Tickets sind sortiert", author: "Anna" }),
];

function post(body: unknown, cookie = "OPENSMESTATE=t"): Request {
  return new Request("http://portal.local/api/ai/summarize", {
    method: "POST",
    headers: { "Content-Type": "application/json", cookie },
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

describe("POST/GET /api/ai/summarize", () => {
  beforeEach(() => {
    vi.stubEnv("OLLAMA_URL", "http://ai:11434");
    vi.stubEnv("BACKEND_URL", "http://portal:8080");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("GET probe: 503 {enabled:false} when OLLAMA_URL is unset", async () => {
    vi.stubEnv("OLLAMA_URL", "");
    const res = await GET();
    expect(res.status).toBe(503);
    await expect(res.json()).resolves.toEqual({ enabled: false });
  });

  it("GET probe: 200 {enabled:true} when configured", async () => {
    const res = await GET();
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ enabled: true });
  });

  it("POST: 503 {enabled:false} even before touching the body when disabled", async () => {
    vi.stubEnv("OLLAMA_URL", "");
    const res = await POST(post({ threadId: 7 }));
    expect(res.status).toBe(503);
    await expect(res.json()).resolves.toEqual({ enabled: false });
  });

  it("POST: 400 on a missing/invalid threadId", async () => {
    for (const body of [null, {}, { threadId: "7" }, { threadId: -1 }]) {
      const res = await POST(post(body));
      expect(res.status).toBe(400);
      const data = (await res.json()) as { error?: string };
      expect(data.error).toBeTruthy();
    }
  });

  it("POST: happy path — 200 {summary} from the mocked LLM", async () => {
    let chatBody: { model: string; messages: { content: string }[] } | undefined;
    vi.stubGlobal(
      "fetch",
      urlRoutedFetch({
        "/api/intercom": () =>
          new Response(JSON.stringify({ messages: NOTES, latest: 7 }), { status: 200 }),
        "/api/chat": (init) => {
          chatBody = JSON.parse(String(init?.body));
          return new Response(
            JSON.stringify({ message: { content: "Bob deployt, Anna sortiert Tickets." } }),
            { status: 200 },
          );
        },
      }),
    );
    const res = await POST(post({ threadId: 7 }));
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({
      summary: "Bob deployt, Anna sortiert Tickets.",
    });
    expect(chatBody?.model).toBeTruthy();
    expect(chatBody?.messages[1].content).toContain("Deploy steht");
  });

  it("POST: forwards the caller's cookie to the intercom backend", async () => {
    let cookieSeen: string | null = null;
    vi.stubGlobal(
      "fetch",
      urlRoutedFetch({
        "/api/intercom": (init) => {
          cookieSeen = new Headers(init?.headers).get("cookie");
          return new Response(JSON.stringify({ messages: NOTES, latest: 7 }), { status: 200 });
        },
        "/api/chat": () =>
          new Response(JSON.stringify({ message: { content: "ok" } }), { status: 200 }),
      }),
    );
    await POST(post({ threadId: 6 }, "OPENSMESTATE=abc"));
    expect(cookieSeen).toBe("OPENSMESTATE=abc");
  });

  it("POST: 404 when the threadId is unknown", async () => {
    vi.stubGlobal(
      "fetch",
      urlRoutedFetch({
        "/api/intercom": () =>
          new Response(JSON.stringify({ messages: NOTES, latest: 7 }), { status: 200 }),
      }),
    );
    const res = await POST(post({ threadId: 99 }));
    expect(res.status).toBe(404);
  });

  it("POST: 401 propagates from the backend session check", async () => {
    vi.stubGlobal(
      "fetch",
      urlRoutedFetch({ "/api/intercom": () => new Response("", { status: 401 }) }),
    );
    const res = await POST(post({ threadId: 7 }));
    expect(res.status).toBe(401);
  });

  it("POST: 502 with the graceful message when the LLM is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      urlRoutedFetch({
        "/api/intercom": () =>
          new Response(JSON.stringify({ messages: NOTES, latest: 7 }), { status: 200 }),
        "/api/chat": () => {
          throw new TypeError("fetch failed");
        },
      }),
    );
    const res = await POST(post({ threadId: 7 }));
    expect(res.status).toBe(502);
    const data = (await res.json()) as { error?: string };
    expect(data.error).toBe("KI nicht erreichbar");
  });
});
