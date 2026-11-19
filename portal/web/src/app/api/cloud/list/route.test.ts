import { GET } from "./route";
import type { CloudFile } from "@/lib/clouddav";

/**
 * Route tests for GET /api/cloud/list: with no DAV credentials in the
 * web environment the route delegates to the Rust bridge
 * `{BACKEND_URL}/api/cloud/files/metadata` — which sits behind the
 * portal's session firewall. The user's session cookie MUST be forwarded
 * or the bridge answers 401 (found live 2026-11: the picker always
 * surfaced "Cloud-Liste fehlgeschlagen (HTTP 401)").
 */

function urlRoutedFetch(handlers: Record<string, (init?: RequestInit) => Response>) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    // strip the query string: the bridge URL carries ?path=…
    const path = url.split("?")[0];
    const handler = Object.entries(handlers).find(([suffix]) => path.endsWith(suffix));
    if (!handler) throw new Error(`unexpected fetch: ${url}`);
    return handler[1](init);
  });
}

function bridgeFiles(): CloudFile[] {
  return [
    { name: "bericht.pdf", size: 2048, mime: "application/pdf", path: "bericht.pdf" },
    { name: "skaizze.txt", size: 12, mime: "text/plain", path: "skaizze.txt" },
  ];
}

/** Case-insensitive header lookup on the raw fetch init (plain object). */
function initHeader(init: RequestInit | undefined, name: string): string | null {
  try {
    return new Headers((init?.headers as HeadersInit | undefined) ?? {}).get(name);
  } catch {
    return null;
  }
}

describe("GET /api/cloud/list (bridge delegation)", () => {
  beforeEach(() => {
    vi.stubEnv("OPENCLOUD_WEBDAV_URL", "");
    vi.stubEnv("CLOUD_WEBDAV_URL", "");
    vi.stubEnv("OPENCLOUD_URL", "");
    vi.stubEnv("OPENCLOUD_DOMAIN", "");
    vi.stubEnv("CLOUD_WEBDAV_USER", "");
    vi.stubEnv("CLOUD_WEBDAV_PASS", "");
    vi.stubEnv("BACKEND_URL", "http://portal:8080");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("forwards the inbound session cookie to the Rust bridge", async () => {
    let forwardedCookie: string | null = null;
    const fetchStub = urlRoutedFetch({
      "/api/cloud/files/metadata": (init) => {
        forwardedCookie = initHeader(init, "cookie");
        return new Response(
          JSON.stringify({ files: bridgeFiles().map((f) => ({ name: f.name, size: f.size, content_type: f.mime })) }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    vi.stubGlobal("fetch", fetchStub);
    const req = new Request("http://portal.local/api/cloud/list?path=", {
      headers: { cookie: "OPENSMESTATE=abc123" },
    });
    const res = await GET(req);
    expect(res.status).toBe(200);
    expect(forwardedCookie).toBe("OPENSMESTATE=abc123");
  });

  it("no cookie present -> bridge call carries no cookie header", async () => {
    const fetchStub = urlRoutedFetch({
      "/api/cloud/files/metadata": (init) => {
        expect(initHeader(init, "cookie")).toBeNull();
        return new Response(JSON.stringify({ files: [] }), { status: 200, headers: { "Content-Type": "application/json" } });
      },
    });
    vi.stubGlobal("fetch", fetchStub);
    // no cookie on the inbound request
    const res = await GET(new Request("http://portal.local/api/cloud/list"));
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual([]);
  });

  it("map bridge failure onto 502 with the upstream status in German", async () => {
    const fetchStub = urlRoutedFetch({
      "/api/cloud/files/metadata": () => new Response("denied", { status: 401 }),
    });
    vi.stubGlobal("fetch", fetchStub);
    const res = await GET(new Request("http://portal.local/api/cloud/list", { headers: { cookie: "OPENSMESTATE=abc123" } }));
    expect(res.status).toBe(502);
    await expect(res.json()).resolves.toEqual({
      error: "Cloud-Liste fehlgeschlagen (HTTP 401)",
    });
  });

  it("happy path: bridge file list maps to CloudFile[] (files only, sorted)", async () => {
    const raw = [
      { name: "ordner", is_directory: true, size: null, content_type: null },
      { name: "skaizze.txt", size: 12, content_type: "text/plain" },
      { name: "bericht.pdf", size: 2048, content_type: "application/pdf" },
    ];
    const fetchStub = urlRoutedFetch({
      "/api/cloud/files/metadata": () =>
        new Response(JSON.stringify({ files: raw }), { status: 200, headers: { "Content-Type": "application/json" } }),
    });
    vi.stubGlobal("fetch", fetchStub);
    const res = await GET(new Request("http://portal.local/api/cloud/list", { headers: { cookie: "OPENSMESTATE=x" } }));
    expect(res.status).toBe(200);
    const files = (await res.json()) as CloudFile[];
    expect(files.map((f) => f.name)).toEqual(["bericht.pdf", "skaizze.txt"]);
    expect(files[0].path).toBe("bericht.pdf");
    expect(files[0].mime).toBe("application/pdf");
  });
});
