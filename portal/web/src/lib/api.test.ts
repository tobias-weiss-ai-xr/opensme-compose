import { afterEach, describe, expect, it, vi } from "vitest";
import {
  API_BASE,
  chatWithAi,
  fetchAnnouncements,
  fetchFeeds,
  fetchIntercom,
  fetchServices,
  postIntercom,
  postIntercomUpload,
} from "./api";

/**
 * api.ts is a same-origin client: under jsdom (window defined) API_BASE is ""
 * so every GET resolves to the bare /api/* path. fetch is stubbed globally
 * with Response objects (undici Response is available in Node 20+ even in
 * the jsdom environment).
 */
afterEach(() => {
  vi.unstubAllGlobals();
});

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

describe("API_BASE", () => {
  it("is '' under jsdom (window defined → same-origin paths)", () => {
    expect(typeof window).toBe("object");
    expect(API_BASE).toBe("");
  });
});

describe("fetchServices", () => {
  it("calls GET /api/services with cache no-store and resolves parsed json on 200", async () => {
    const services = [{ id: "s1", name: "Mail", description: "d", url: "https://m" }];
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ services }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchServices();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/services",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result).toEqual({ services });
    expect(result.services[0].name).toBe("Mail");
  });

  it("throws `HTTP 500 /api/services` on 500", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("boom", { status: 500 })));

    await expect(fetchServices()).rejects.toThrow("HTTP 500 /api/services");
  });
});

describe("fetchAnnouncements", () => {
  it("calls GET /api/announcements and resolves the list", async () => {
    const announcements = [{ level: "warn", text: "maintenance" }];
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ announcements }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchAnnouncements();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/announcements",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result.announcements).toEqual(announcements);
  });
});

describe("fetchFeeds (failure-swallowing)", () => {
  it("parses the FeedResponse columns on 200", async () => {
    const columns = [{ id: "c1", title: "News", entries: [] }];
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ columns }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchFeeds();

    expect(fetchMock).toHaveBeenCalledWith("/api/feeds", expect.anything());
    expect(result.columns).toEqual(columns);
  });

  it("resolves {columns: []} when fetch rejects", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("network down")));

    await expect(fetchFeeds()).resolves.toEqual({ columns: [] });
  });

  it("resolves {columns: []} when the backend answers 500", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("oops", { status: 500 })));

    await expect(fetchFeeds()).resolves.toEqual({ columns: [] });
  });
});

describe("fetchIntercom", () => {
  it("calls GET /api/intercom and resolves the message list", async () => {
    const messages = [{ text: "hello", attachment: null }];
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ messages }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchIntercom();

    expect(fetchMock).toHaveBeenCalledWith("/api/intercom", expect.anything());
    expect(result.messages).toEqual(messages);
  });
});

describe("postIntercom", () => {
  it("POSTs JSON body and resolves the response json", async () => {
    const created = { text: "hi", attachment: null };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(created, 201));
    vi.stubGlobal("fetch", fetchMock);

    const result = await postIntercom({ text: "hi" });

    expect(fetchMock).toHaveBeenCalledWith("/api/intercom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: "hi" }),
    });
    expect(result).toEqual(created);
  });

  it("throws the JSON error message from a non-ok response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ error: "attachment too large" }, 413)),
    );

    await expect(postIntercom({ text: "x" })).rejects.toThrow("attachment too large");
  });

  it("falls back to `HTTP <status>` when the error body is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("plain", { status: 502 })));

    await expect(postIntercom({ text: "x" })).rejects.toThrow("HTTP 502");
  });
});

describe("postIntercomUpload", () => {
  it("builds an encoded query string, sends the raw File as body with its content-type", async () => {
    const file = new File([new Uint8Array([1, 2, 3])], "my file.png", { type: "image/png" });
    const created = { text: "hello & world", attachment: { url: "/f/1", name: "my file.png", size: 3, content_type: "image/png" } };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(created, 201));
    vi.stubGlobal("fetch", fetchMock);

    const result = await postIntercomUpload("hello & world", file);

    expect(fetchMock).toHaveBeenCalledWith(
      `/api/intercom/upload?text=${encodeURIComponent("hello & world")}&filename=${encodeURIComponent(file.name)}`,
      {
        method: "POST",
        headers: { "Content-Type": "image/png" },
        body: file,
      },
    );
    expect(result).toEqual(created);
  });

  it("defaults the content-type to application/octet-stream when the file has none", async () => {
    const file = new File([new Uint8Array([9])], "blob.bin");
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ text: "t", attachment: null }, 201));
    vi.stubGlobal("fetch", fetchMock);

    await postIntercomUpload("t", file);

    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/api/intercom/upload"), {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
  });

  it("throws the JSON error message from a non-ok upload response", async () => {
    const file = new File([new Uint8Array([1])], "a.png", { type: "image/png" });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ error: "virus detected" }, 422)),
    );

    await expect(postIntercomUpload("t", file)).rejects.toThrow("virus detected");
  });
});

describe("chatWithAi", () => {
  it("POSTs the request as JSON and resolves the answer", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ answer: "42" }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await chatWithAi({ question: "meaning of life?" });

    expect(fetchMock).toHaveBeenCalledWith("/api/ai/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: "meaning of life?" }),
    });
    expect(result.answer).toBe("42");
  });

  it("throws `HTTP <status>` on non-ok", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("overloaded", { status: 503 })));

    await expect(chatWithAi({ question: "q" })).rejects.toThrow("HTTP 503");
  });
});
