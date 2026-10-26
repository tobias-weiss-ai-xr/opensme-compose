import { afterEach, describe, expect, it, vi } from "vitest";
import { backendUrl, forwardToBackend } from "./backend";

const ORIG = process.env.BACKEND_URL;

afterEach(() => {
  if (ORIG === undefined) delete process.env.BACKEND_URL;
  else process.env.BACKEND_URL = ORIG;
  vi.restoreAllMocks();
});

describe("backendUrl (runtime env, not build-time inlined)", () => {
  it("falls back to localhost:8080 when env unset", () => {
    delete process.env.BACKEND_URL;
    expect(backendUrl()).toBe("http://localhost:8080");
  });

  it("reads the runtime value", () => {
    process.env.BACKEND_URL = "http://portal:8080";
    expect(backendUrl()).toBe("http://portal:8080");
  });
});

describe("forwardToBackend", () => {
  it("echoes upstream status, content-type and body", async () => {
    const upstream = new Response(JSON.stringify({ ok: true }), {
      status: 201,
      headers: { "content-type": "application/json", "x-ignore": "nope" },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(upstream));

    const result = await forwardToBackend({
      url: "http://portal:8080/api/intercom",
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });

    expect(result.status).toBe(201);
    expect(result.headers.get("content-type")).toBe("application/json");
    expect(result.headers.get("x-ignore")).toBeNull();
    expect(result.headers.get("cache-control")).toBe("no-store");
    expect(JSON.parse(Buffer.from(result.bytes).toString("utf-8"))).toEqual({ ok: true });
  });

  it("propagates upstream 404 (passthrough, not a portal error)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("nf", { status: 404 })),
    );
    const result = await forwardToBackend({ url: "http://portal:8080/api/x", method: "GET" });
    expect(result.status).toBe(404);
  });

  it("echoes the logout contract: 303 + Location + Set-Cookie", async () => {
    // POST /api/logout → 303 redirect to IdP end-session + session-cookie clear
    const upstreamHeaders = new Headers({
      location: "https://auth.example/oidc/v1/end_session?post_logout_redirect_uri=https://home.example/logged-out",
      "content-type": "text/plain",
    });
    upstreamHeaders.append("set-cookie", "OPENSMESESSION=; Path=/; HttpOnly; Max-Age=0");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("redir", { status: 303, headers: upstreamHeaders }),
      ),
    );

    const result = await forwardToBackend({
      url: "http://portal:8080/api/logout",
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: "",
    });

    expect(result.status).toBe(303);
    expect(result.headers.get("location")).toContain("end_session");
    const cookies = result.headers.getSetCookie
      ? result.headers.getSetCookie()
      : [result.headers.get("set-cookie") ?? ""];
    expect(cookies.join("|")).toContain("OPENSMESESSION=; Path=/; HttpOnly; Max-Age=0");
  });
});
