import {
  LlmError,
  ThreadFetchError,
  aiEnabled,
  aiModel,
  aiTimeoutMs,
  buildTranscript,
  draftReplyMessages,
  fetchIntercomThread,
  ollamaChat,
  ollamaUrl,
  runAiHelper,
  summarizeMessages,
} from "./llm";
import type { ChatMessage } from "./llm";
import type { IntercomMessage } from "./types";

function note(partial: Partial<IntercomMessage> & { id: number; text: string }): IntercomMessage {
  return { attachment: null, ...partial };
}

const NOTES: IntercomMessage[] = [
  note({ id: 3, text: "Release ist eingecheckt", author: "Bob" }),
  note({ id: 2, text: "Kunde wartet auf Feedback", author: "Anna" }),
  note({ id: 1, text: "Kickoff notiert" }),
];

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Fetch double that answers by URL — intercom list + ollama chat. */
function urlRoutedFetch(handlers: Record<string, (init?: RequestInit) => Response>) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const handler = Object.entries(handlers).find(([suffix]) => url.endsWith(suffix));
    if (!handler) throw new Error(`unexpected fetch: ${url}`);
    return handler[1](init);
  });
}

describe("AI gate + config (OLLAMA_URL / AI_MODEL)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("is disabled when OLLAMA_URL is unset or blank", () => {
    vi.stubEnv("OLLAMA_URL", "");
    expect(aiEnabled()).toBe(false);
    vi.stubEnv("OLLAMA_URL", "   ");
    expect(aiEnabled()).toBe(false);
  });

  it("is enabled when the operator sets OLLAMA_URL", () => {
    vi.stubEnv("OLLAMA_URL", "http://ai:11434");
    expect(aiEnabled()).toBe(true);
    expect(ollamaUrl()).toBe("http://ai:11434");
  });

  it("defaults the endpoint to http://ai:11434 and strips trailing slashes", () => {
    vi.stubEnv("OLLAMA_URL", "");
    expect(ollamaUrl()).toBe("http://ai:11434");
    vi.stubEnv("OLLAMA_URL", "http://gpu-host:11434/");
    expect(ollamaUrl()).toBe("http://gpu-host:11434");
  });

  it("reads the model from AI_MODEL, falls back to OLLAMA_MODEL then llama3.1", () => {
    vi.stubEnv("AI_MODEL", "");
    vi.stubEnv("OLLAMA_MODEL", "");
    expect(aiModel()).toBe("llama3.1");
    vi.stubEnv("OLLAMA_MODEL", "qwen3-30b-a3b:latest");
    expect(aiModel()).toBe("qwen3-30b-a3b:latest");
    vi.stubEnv("AI_MODEL", "llama3.2");
    expect(aiModel()).toBe("llama3.2");
  });

  it("defaults the timeout to 120s and honors AI_TIMEOUT_MS", () => {
    vi.stubEnv("AI_TIMEOUT_MS", "");
    expect(aiTimeoutMs()).toBe(120_000);
    vi.stubEnv("AI_TIMEOUT_MS", "5000");
    expect(aiTimeoutMs()).toBe(5000);
    vi.stubEnv("AI_TIMEOUT_MS", "nope");
    expect(aiTimeoutMs()).toBe(120_000);
  });
});

describe("ollamaChat", () => {
  beforeEach(() => {
    vi.stubEnv("OLLAMA_URL", "http://ai:11434");
    vi.stubEnv("AI_MODEL", "llama3.2");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  const MESSAGES: ChatMessage[] = [
    { role: "system", content: "Sei knapp." },
    { role: "user", content: "Fasse zusammen." },
  ];

  it("POSTs the ollama chat contract and resolves the content (success path)", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ message: { role: "assistant", content: "  Kurze Zusammenfassung.  " } }),
    );
    await expect(ollamaChat(MESSAGES, { fetchImpl })).resolves.toBe("Kurze Zusammenfassung.");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://ai:11434/api/chat");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      model: "llama3.2",
      messages: MESSAGES,
      stream: false,
    });
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("maps a timeout abort to a graceful LlmError mentioning the timeout", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new DOMException("The operation timed out", "TimeoutError");
    });
    await expect(ollamaChat(MESSAGES, { fetchImpl })).rejects.toMatchObject({
      name: "LlmError",
      message: expect.stringContaining("Timeout"),
    });
  });

  it("maps an unreachable endpoint to a graceful LlmError", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    await expect(ollamaChat(MESSAGES, { fetchImpl })).rejects.toMatchObject({
      name: "LlmError",
      message: "KI nicht erreichbar",
    });
  });

  it("surfaces an upstream HTTP error with its error field", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: "model 'x' not found" }, 404),
    );
    await expect(ollamaChat(MESSAGES, { fetchImpl })).rejects.toMatchObject({
      name: "LlmError",
      message: "KI-Antwort fehlgeschlagen (model 'x' not found)",
    });
  });

  it("rejects a non-JSON answer body", async () => {
    const fetchImpl = vi.fn(async () => new Response("gateway garbage", { status: 200 }));
    await expect(ollamaChat(MESSAGES, { fetchImpl })).rejects.toMatchObject({
      name: "LlmError",
      message: expect.stringContaining("kein JSON"),
    });
  });

  it("rejects an empty answer", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ message: { content: "   " } }));
    await expect(ollamaChat(MESSAGES, { fetchImpl })).rejects.toMatchObject({
      name: "LlmError",
      message: "KI lieferte eine leere Antwort",
    });
  });
});

describe("fetchIntercomThread", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("GETs the backend intercom list with the caller's cookie", async () => {
    vi.stubEnv("BACKEND_URL", "http://portal:8080");
    const fetchMock = vi.fn(async () => jsonResponse({ messages: NOTES, latest: 3 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchIntercomThread("OPENSMESTATE=abc")).resolves.toEqual(NOTES);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://portal:8080/api/intercom");
    expect(init.method).toBe("GET");
    expect(new Headers(init.headers).get("cookie")).toBe("OPENSMESTATE=abc");
  });

  it("propagates 401 as ThreadFetchError(status 401)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 401 })));
    await expect(fetchIntercomThread(null)).rejects.toMatchObject({
      name: "ThreadFetchError",
      status: 401,
    });
  });

  it("maps backend failures to their HTTP status and unreachable to 502", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("", { status: 503 })),
    );
    await expect(fetchIntercomThread(null)).rejects.toMatchObject({
      name: "ThreadFetchError",
      status: 503,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );
    await expect(fetchIntercomThread(null)).rejects.toBeInstanceOf(ThreadFetchError);
    await expect(fetchIntercomThread(null)).rejects.toMatchObject({ status: 502 });
  });

  it("rejects a malformed list payload as 502", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ nope: true })));
    await expect(fetchIntercomThread(null)).rejects.toMatchObject({ status: 502 });
  });
});

describe("buildTranscript + prompts", () => {
  it("renders notes as [#id] author: text lines with an author fallback", () => {
    const transcript = buildTranscript(NOTES, 3);
    expect(transcript.split("\n")).toEqual([
      "[#3] Bob: Release ist eingecheckt  ← Antwort-Ziel",
      "[#2] Anna: Kunde wartet auf Feedback",
      "[#1] unbekannt: Kickoff notiert",
    ]);
  });

  it("caps the transcript at the configured number of notes", () => {
    const many = Array.from({ length: 60 }, (_, i) =>
      note({ id: 60 - i, text: `Notiz ${60 - i}` }),
    );
    const lines = buildTranscript(many, 60).split("\n");
    expect(lines).toHaveLength(40);
    expect(lines[0]).toContain("Notiz 60");
  });

  it("summarize prompt demands a German summary-only answer", () => {
    const msgs = summarizeMessages("[#1] a: hallo");
    expect(msgs).toHaveLength(2);
    expect(msgs[0].role).toBe("system");
    expect(msgs[0].content).toContain("Deutsch");
    expect(msgs[1].content).toContain("[#1] a: hallo");
  });

  it("draft prompt anchors the reply on the marked target note", () => {
    const msgs = draftReplyMessages(buildTranscript(NOTES, 2));
    expect(msgs[0].role).toBe("system");
    expect(msgs[1].content).toContain("← Antwort-Ziel");
    expect(msgs[1].content).toContain("[#2] Anna: Kunde wartet auf Feedback");
  });
});

describe("runAiHelper (shared summarize/draft pipeline)", () => {
  beforeEach(() => {
    vi.stubEnv("OLLAMA_URL", "http://ai:11434");
    vi.stubEnv("BACKEND_URL", "http://portal:8080");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  function stubBackend(handlers: Record<string, (init?: RequestInit) => Response>) {
    vi.stubGlobal("fetch", urlRoutedFetch(handlers));
  }

  it("rejects a missing/invalid threadId with 400 and no fetches", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    for (const body of [null, {}, { threadId: "3" }, { threadId: 0 }, { threadId: 1.5 }]) {
      const res = await runAiHelper("summary", { body, cookie: null });
      expect(res.status).toBe(400);
      expect(res.payload).toEqual({ error: "threadId fehlt oder ungültig" });
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("summarizes: thread fetch → transcript → chat → {summary}", async () => {
    let chatInit: RequestInit | undefined;
    stubBackend({
      "/api/intercom": () => jsonResponse({ messages: NOTES, latest: 3 }),
      "/api/chat": (init) => {
        chatInit = init;
        return jsonResponse({ message: { content: "Bob hat eingecheckt." } });
      },
    });
    const res = await runAiHelper("summary", {
      body: { threadId: 3 },
      cookie: "OPENSMESTATE=t",
    });
    expect(res).toEqual({ status: 200, payload: { summary: "Bob hat eingecheckt." } });
    const body = JSON.parse(String(chatInit?.body)) as {
      messages: ChatMessage[];
    };
    expect(body.messages[1].content).toContain("Release ist eingecheckt");
  });

  it("drafts: returns {draft} and marks the target note in the transcript", async () => {
    let chatBody: { messages: ChatMessage[] } | undefined;
    stubBackend({
      "/api/intercom": () => jsonResponse({ messages: NOTES, latest: 3 }),
      "/api/chat": (init) => {
        chatBody = JSON.parse(String(init?.body));
        return jsonResponse({ message: { content: "Danke Bob!" } });
      },
    });
    const res = await runAiHelper("draft", { body: { threadId: 3 }, cookie: null });
    expect(res).toEqual({ status: 200, payload: { draft: "Danke Bob!" } });
    expect(chatBody?.messages[1].content).toContain("← Antwort-Ziel");
  });

  it("answers 404 when the threadId is not in the fetched list", async () => {
    stubBackend({ "/api/intercom": () => jsonResponse({ messages: NOTES, latest: 3 }) });
    const res = await runAiHelper("summary", { body: { threadId: 99 }, cookie: null });
    expect(res.status).toBe(404);
    expect(res.payload).toEqual({ error: "Notiz nicht gefunden" });
  });

  it("propagates the backend status (401 session hop)", async () => {
    stubBackend({ "/api/intercom": () => new Response("", { status: 401 }) });
    const res = await runAiHelper("summary", { body: { threadId: 3 }, cookie: "x" });
    expect(res.status).toBe(401);
    expect(res.payload).toEqual({ error: "unauthorized" });
  });

  it("maps an LlmError to 502 with its graceful message", async () => {
    stubBackend({
      "/api/intercom": () => jsonResponse({ messages: NOTES, latest: 3 }),
      "/api/chat": () => {
        throw new LlmError("KI nicht erreichbar");
      },
    });
    const res = await runAiHelper("summary", { body: { threadId: 3 }, cookie: null });
    expect(res.status).toBe(502);
    expect(res.payload).toEqual({ error: "KI nicht erreichbar" });
  });

  it("falls back to a generic 502 for unexpected chat errors", async () => {
    stubBackend({
      "/api/intercom": () => jsonResponse({ messages: NOTES, latest: 3 }),
      "/api/chat": () => {
        throw new Error("boom");
      },
    });
    const res = await runAiHelper("draft", { body: { threadId: 3 }, cookie: null });
    expect(res.status).toBe(502);
    expect(res.payload).toEqual({ error: "KI nicht erreichbar" });
  });
});
