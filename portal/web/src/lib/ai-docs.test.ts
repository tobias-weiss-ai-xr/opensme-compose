import { GET, POST } from "@/app/api/ai/docs/route";
import { docMessages, runDocAnalysis } from "./ai-docs";

/**
 * Route-logic tests for the KI-Analyse pipeline (lib/ai-docs.ts +
 * /api/ai/docs): the OLLAMA_URL gate (503 {enabled:false}), {path}
 * validation (no ../, leading / stripped), the WebDAV download via a
 * URL-routed fetch stub, the 422 for files without extractable text,
 * and the mocked-LLM happy path (200 {answer}) — no live AI, no cloud.
 */

const DAV_ROOT = "https://cloud.example/remote.php/dav/files";

function textResponse(body: string, mime = "text/plain"): Response {
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": mime },
  });
}

function urlRoutedFetch(
  handlers: Record<string, (init?: RequestInit) => Response>,
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const handler = Object.entries(handlers).find(([suffix]) => url.endsWith(suffix));
    if (!handler) throw new Error(`unexpected fetch: ${url}`);
    return handler[1](init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function post(body: unknown): Request {
  return new Request("http://portal.local/api/ai/docs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("runDocAnalysis (route logic)", () => {
  beforeEach(() => {
    vi.stubEnv("OLLAMA_URL", "http://ai:11434");
    vi.stubEnv("OPENCLOUD_WEBDAV_URL", DAV_ROOT);
    vi.stubEnv("OPENCLOUD_ADMIN_USER", "admin");
    vi.stubEnv("OPENCLOUD_ADMIN_PASSWORD", "secret");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("503 {enabled:false} without touching fetch when OLLAMA_URL is unset", async () => {
    vi.stubEnv("OLLAMA_URL", "");
    const fetchMock = urlRoutedFetch({});
    const res = await runDocAnalysis({ body: { path: "a.txt" } });
    expect(res.status).toBe(503);
    expect(res.payload).toEqual({ enabled: false });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("400 on a missing/invalid path (no body, wrong type, empty, .. escape)", async () => {
    const fetchMock = urlRoutedFetch({});
    for (const body of [
      null,
      {},
      { path: "" },
      { path: "///" },
      { path: "../secret.txt" },
      { path: "docs/../../etc/passwd" },
      { path: 42 },
    ]) {
      const res = await runDocAnalysis({ body });
      expect(res.status).toBe(400);
      expect(res.payload.error).toBeTruthy();
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("200 {answer} happy path — downloads the file and summarizes", async () => {
    let chatBody: { model: string; messages: { content: string }[] } | undefined;
    let downloadUrl = "";
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith(DAV_ROOT)) {
        downloadUrl = url;
        return textResponse("Quartalsbericht: Umsatz steigt, Kosten sinken.");
      }
      if (url.endsWith("/api/chat")) {
        chatBody = JSON.parse(String(init?.body));
        return new Response(
          JSON.stringify({ message: { content: "Der Umsatz steigt bei sinkenden Kosten." } }),
          { status: 200 },
        );
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const res = await runDocAnalysis({ body: { path: "docs/bericht.txt" } });
    expect(res.status).toBe(200);
    expect(res.payload).toEqual({
      answer: "Der Umsatz steigt bei sinkenden Kosten.",
    });
    expect(downloadUrl).toBe(`${DAV_ROOT}/admin/docs/bericht.txt`);
    expect(chatBody?.model).toBeTruthy();
    expect(chatBody?.messages[1].content).toContain(
      "Fasse das Dokument in 3-5 deutschen Sätzen zusammen",
    );
    expect(chatBody?.messages[1].content).toContain("Umsatz steigt");
  });

  it("embeds the question when one is given (answer-with-context mode)", async () => {
    let chatBody: { messages: { content: string }[] } | undefined;
    urlRoutedFetch({
      "/dav/files/admin/notiz.md": () =>
        textResponse("# Notiz\nThema: Budget", "text/markdown"),
      "/api/chat": (init) => {
        chatBody = JSON.parse(String(init?.body));
        return new Response(
          JSON.stringify({ message: { content: "Das Budget ist knapp." } }),
          { status: 200 },
        );
      },
    });
    const res = await runDocAnalysis({
      body: { path: "notiz.md", question: "Worum geht es?" },
    });
    expect(res.status).toBe(200);
    expect(res.payload).toEqual({ answer: "Das Budget ist knapp." });
    expect(chatBody?.messages[1].content).toContain("Worum geht es?");
    expect(chatBody?.messages[1].content).not.toContain(
      "Fasse das Dokument in 3-5",
    );
  });

  it("strips a leading slash from the path before the download", async () => {
    let downloadUrl = "";
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith(DAV_ROOT)) {
        downloadUrl = url;
        return textResponse("inhalt");
      }
      if (url.endsWith("/api/chat")) {
        return new Response(JSON.stringify({ message: { content: "ok" } }), {
          status: 200,
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const res = await runDocAnalysis({ body: { path: "/docs/a.txt" } });
    expect(res.status).toBe(200);
    expect(downloadUrl).toBe(`${DAV_ROOT}/admin/docs/a.txt`);
  });

  it("422 'Dateityp nicht unterstützbar' for binaries (pdf) and empty text", async () => {
    const fetchMock = urlRoutedFetch({
      "/dav/files/admin/bericht.pdf": () =>
        textResponse("%PDF-1.4 binary…", "application/pdf"),
      "/dav/files/admin/leer.txt": () => textResponse("   "),
    });
    const pdf = await runDocAnalysis({ body: { path: "bericht.pdf" } });
    expect(pdf.status).toBe(422);
    expect(pdf.payload).toEqual({ error: "Dateityp nicht unterstützbar" });
    const leer = await runDocAnalysis({ body: { path: "leer.txt" } });
    expect(leer.status).toBe(422);
    expect(leer.payload).toEqual({ error: "Dateityp nicht unterstützbar" });
    // neither reached the LLM — only the two DAV downloads happened
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("502 with the graceful message when the cloud is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );
    const res = await runDocAnalysis({ body: { path: "a.txt" } });
    expect(res.status).toBe(502);
    expect(res.payload).toEqual({ error: "Cloud nicht erreichbar" });
  });

  it("502 when the DAV download answers non-2xx", async () => {
    urlRoutedFetch({
      "/dav/files/admin/fehlt.txt": () => new Response("", { status: 404 }),
    });
    const res = await runDocAnalysis({ body: { path: "fehlt.txt" } });
    expect(res.status).toBe(502);
    expect(res.payload).toEqual({
      error: "Cloud-Download fehlgeschlagen (HTTP 404)",
    });
  });

  it("502 with the graceful message when the LLM is unreachable", async () => {
    urlRoutedFetch({
      "/dav/files/admin/a.txt": () => textResponse("Text"),
      "/api/chat": () => {
        throw new TypeError("fetch failed");
      },
    });
    const res = await runDocAnalysis({ body: { path: "a.txt" } });
    expect(res.status).toBe(502);
    expect(res.payload).toEqual({ error: "KI nicht erreichbar" });
  });
});

describe("GET/POST /api/ai/docs", () => {
  beforeEach(() => {
    vi.stubEnv("OLLAMA_URL", "http://ai:11434");
    vi.stubEnv("OPENCLOUD_WEBDAV_URL", DAV_ROOT);
    vi.stubEnv("OPENCLOUD_ADMIN_USER", "admin");
    vi.stubEnv("OPENCLOUD_ADMIN_PASSWORD", "secret");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("GET probe: 503 {enabled:false} when OLLAMA_URL is unset, else {enabled:true}", async () => {
    vi.stubEnv("OLLAMA_URL", "");
    expect((await GET()).status).toBe(503);
    await expect((await GET()).json()).resolves.toEqual({ enabled: false });
    vi.stubEnv("OLLAMA_URL", "http://ai:11434");
    expect((await GET()).status).toBe(200);
    await expect((await GET()).json()).resolves.toEqual({ enabled: true });
  });

  it("POST: 503 {enabled:false} even before reading the body when disabled", async () => {
    vi.stubEnv("OLLAMA_URL", "");
    const res = await POST(post({ path: "a.txt" }));
    expect(res.status).toBe(503);
    await expect(res.json()).resolves.toEqual({ enabled: false });
  });

  it("POST: delegates to the pipeline — 400 on an unreadable body", async () => {
    const req = new Request("http://portal.local/api/ai/docs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "not json",
    });
    const res = await POST(req);
    expect(res.status).toBe(400);
    const data = (await res.json()) as { error?: string };
    expect(data.error).toBeTruthy();
  });

  it("POST: happy path end-to-end through the route", async () => {
    urlRoutedFetch({
      "/dav/files/admin/docs/a.txt": () => textResponse("Hallo Welt"),
      "/api/chat": () =>
        new Response(
          JSON.stringify({ message: { content: "Kurze Zusammenfassung." } }),
          { status: 200 },
        ),
    });
    const res = await POST(post({ path: "docs/a.txt" }));
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ answer: "Kurze Zusammenfassung." });
  });
});

describe("docMessages", () => {
  it("default mode uses the exact German summary instruction", () => {
    const msgs = docMessages({ name: "a.txt", text: "Text", question: null });
    expect(msgs[0].role).toBe("system");
    expect(msgs[1].content).toContain(
      "Fasse das Dokument in 3-5 deutschen Sätzen zusammen",
    );
    expect(msgs[1].content).toContain('Dokument "a.txt"');
    expect(msgs[1].content).toContain("Text");
  });

  it("question mode answers instead of summarizing", () => {
    const msgs = docMessages({ name: "a.txt", text: "Text", question: "Warum?" });
    expect(msgs[1].content).toContain("Warum?");
    expect(msgs[1].content).not.toContain("Fasse das Dokument");
  });
});
