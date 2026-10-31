import { afterEach, describe, expect, it, vi } from "vitest";
import { probeSession } from "./session";

const jsonRes = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("probeSession (server-side session gate)", () => {
  it("returns true when the backend confirms an authenticated session", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(jsonRes({ authenticated: true, user: { sub: "s1" } }));
    await expect(
      probeSession("http://portal:8080", "OPENSMEPORTALSESSION=abc", { fetchImpl }),
    ).resolves.toBe(true);
  });

  it("forwards the request cookies to the backend", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonRes({ authenticated: true }));
    await probeSession("http://portal:8080", "a=b; c=d", { fetchImpl });
    expect(fetchImpl).toHaveBeenCalledWith(
      "http://portal:8080/api/session",
      expect.objectContaining({
        headers: { cookie: "a=b; c=d" },
        cache: "no-store",
      }),
    );
  });

  it("omits the cookie header when the request carries none", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonRes({ authenticated: false }));
    await probeSession("http://portal:8080", "", { fetchImpl });
    const init = fetchImpl.mock.calls[0][1] as RequestInit;
    expect(init.headers).toBeUndefined();
  });

  it("returns false for an anonymous session", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonRes({ authenticated: false }));
    await expect(
      probeSession("http://portal:8080", "", { fetchImpl }),
    ).resolves.toBe(false);
  });

  it("returns false on a non-OK probe (401/500)", async () => {
    await expect(
      probeSession("http://portal:8080", "", {
        fetchImpl: vi.fn().mockResolvedValue(jsonRes({}, 401)),
      }),
    ).resolves.toBe(false);
    await expect(
      probeSession("http://portal:8080", "", {
        fetchImpl: vi.fn().mockResolvedValue(new Response("boom", { status: 503 })),
      }),
    ).resolves.toBe(false);
  });

  it("is fail-closed when the probe throws (backend down)", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error("backend unreachable"));
    await expect(
      probeSession("http://portal:8080", "", { fetchImpl }),
    ).resolves.toBe(false);
  });

  it("tolerates a trailing slash on the base URL", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonRes({ authenticated: true }));
    await probeSession("http://portal:8080/", "", { fetchImpl });
    expect(fetchImpl.mock.calls[0][0]).toBe("http://portal:8080/api/session");
  });
});
