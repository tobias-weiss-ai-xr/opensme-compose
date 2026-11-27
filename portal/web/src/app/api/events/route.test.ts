import { describe, it, expect, vi, afterEach } from "vitest";
import { GET } from "./route";

vi.mock("@/lib/backend", () => ({
  backendUrl: () => "http://backend.test",
}));

describe("SSE proxy", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("forwards the session cookie and streams the upstream body", async () => {
    const chunks = ["event: x\ndata: {}\n\n"];
    const upstream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const c of chunks) controller.enqueue(new TextEncoder().encode(c));
        controller.close();
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: upstream,
        headers: new Headers({ "content-type": "text/event-stream" }),
      }),
    );
    const req = new Request("http://localhost/api/events", {
      headers: { cookie: "OPENSMEPORTALSESSION=abc", accept: "text/event-stream" },
    });
    const res = await GET(req as never);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    expect(await res.text()).toBe(chunks.join(""));
    const call = vi.mocked(fetch).mock.calls[0][1] as RequestInit;
    expect((call.headers as Headers).get("cookie")).toBe("OPENSMEPORTALSESSION=abc");
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe("http://backend.test/api/events");
  });

  it("forwards a since cursor to the backend", async () => {
    const upstream = new ReadableStream<Uint8Array>({
      start(controller) { controller.close(); },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: upstream,
      headers: new Headers({ "content-type": "text/event-stream" }),
    }));
    const req = new Request("http://localhost/api/events?since=42-1", {
      headers: { cookie: "OPENSMEPORTALSESSION=abc" },
    });
    await GET(req as never);
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
      "http://backend.test/api/events?since=42-1",
    );
  });
});
