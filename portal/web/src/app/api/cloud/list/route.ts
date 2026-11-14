import { backendUrl } from "@/lib/backend";

/**
 * GET /api/cloud/list?path=<relative dir>
 *
 * Lists the files of one cloud directory as a bare JSON array of
 * `{ name, size, mime, path }` (files only, sorted by name) — the
 * contract the CloudFilePicker modal speaks. A static app route takes
 * precedence over the catch-all [[...path]] proxy, so this handler
 * answers instead of forwarding verbatim to the Rust backend.
 *
 * Two sources, tried in order:
 * 1. WebDAV PROPFIND (Depth: 1) with Basic auth straight from the
 *    portal-web environment — used when DAV credentials are present.
 * 2. Rust bridge `GET {BACKEND_URL}/api/cloud/files/metadata` — the
 *    deployed compose chain gives portal-web no DAV secrets, only the
 *    backend URL, so the listing is delegated to the portal service.
 *
 * The returned `path` is relative to the user's DAV files root —
 * exactly what POST /api/cloud/share (share-link creation) expects.
 */

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

interface CloudFile {
  name: string;
  size: number | null;
  mime: string;
  path: string;
}

/** Upstream answered with a non-2xx status — surfaced as a 502. */
class UpstreamError extends Error {
  constructor(readonly status: number) {
    super(`upstream HTTP ${status}`);
  }
}

const PROPFIND_TIMEOUT_MS = 15_000;

const PROPFIND_BODY = `<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:displayname/>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <d:resourcetype/>
    <d:getlastmodified/>
  </d:prop>
</d:propfind>`;

const EXT_MIME: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  svg: "image/svg+xml",
  webp: "image/webp",
  pdf: "application/pdf",
  txt: "text/plain",
  md: "text/markdown",
  json: "application/json",
  csv: "text/csv",
  zip: "application/zip",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
};

function extMime(name: string): string {
  const m = /\.([^.]+)$/.exec(name);
  return (m && EXT_MIME[m[1].toLowerCase()]) || "application/octet-stream";
}

function env(name: string): string | undefined {
  const v = process.env[name];
  return v && v.trim() !== "" ? v.trim() : undefined;
}

/** WebDAV files root, e.g. https://cloud.example/remote.php/dav/files */
function resolveDavBase(): string | null {
  const explicit = env("OPENCLOUD_WEBDAV_URL") ?? env("CLOUD_WEBDAV_URL");
  if (explicit) return explicit.replace(/\/+$/, "");
  const cloudUrl = env("OPENCLOUD_URL");
  if (cloudUrl) return `${cloudUrl.replace(/\/+$/, "")}/remote.php/dav/files`;
  const domain = env("OPENCLOUD_DOMAIN");
  if (domain) {
    const origin = /^https?:\/\//i.test(domain)
      ? domain.replace(/\/+$/, "")
      : `https://${domain}`;
    return `${origin}/remote.php/dav/files`;
  }
  return null;
}

function resolveDavUser(): string | undefined {
  return env("OPENCLOUD_ADMIN_USER") ?? env("CLOUD_WEBDAV_USER");
}

function resolveDavPass(): string | undefined {
  return (
    env("OPENCLOUD_ADMIN_PASSWORD") ??
    env("OPENCLOUD_ADMIN_PASS") ??
    env("CLOUD_WEBDAV_PASS")
  );
}

/** "" (root) is valid; leading/trailing slashes and ./.. segments are not. */
function validateRelPath(raw: string | null): string | null {
  const rel = (raw ?? "").replace(/^\/+/, "").replace(/\/+$/, "");
  if (rel === "") return "";
  for (const seg of rel.split("/")) {
    if (seg === "" || seg === "." || seg === "..") return null;
  }
  return rel;
}

function byName(a: CloudFile, b: CloudFile): number {
  return a.name.localeCompare(b.name, "de", {
    sensitivity: "base",
    numeric: true,
  });
}

/* ── XML helpers (prefix-agnostic — servers use varying namespace prefixes) ── */

function tagPattern(tag: string): RegExp {
  return new RegExp(
    `<(?:[\\w.-]+:)?${tag}\\b[^>]*>([\\s\\S]*?)</(?:[\\w.-]+:)?${tag}>`,
    "gi",
  );
}

function xmlFirst(xml: string, tag: string): string | null {
  const m = tagPattern(tag).exec(xml);
  return m ? m[1] : null;
}

function xmlAll(xml: string, tag: string): string[] {
  const out: string[] = [];
  const re = tagPattern(tag);
  let m: RegExpExecArray | null;
  while ((m = re.exec(xml)) !== null) out.push(m[1]);
  return out;
}

function decodeEntities(s: string): string {
  return s
    .replace(/&#x([0-9a-fA-F]+);/g, (_, h: string) =>
      String.fromCodePoint(parseInt(h, 16)),
    )
    .replace(/&#(\d+);/g, (_, d: string) => String.fromCodePoint(parseInt(d, 10)))
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&"); // last — resolves double escapes
}

/* ── Source 1: direct WebDAV PROPFIND ─────────────────────────────────────── */

function davTargetUrl(base: string, user: string, rel: string): string {
  const cleanBase = base.replace(/\/+$/, "");
  const parts = rel
    ? rel
        .split("/")
        .map(encodeURIComponent)
        .join("/")
    : "";
  return `${cleanBase}/${encodeURIComponent(user)}/${parts ? `${parts}/` : ""}`;
}

/** Decode a multistatus href into a path relative to the user's files root. */
function hrefRel(href: string, basePath: string): string {
  let h = href.trim();
  if (/^https?:\/\//i.test(h)) {
    try {
      h = new URL(h).pathname;
    } catch {
      /* keep raw */
    }
  }
  try {
    h = decodeURIComponent(h);
  } catch {
    /* keep raw — malformed escape */
  }
  if (!h.startsWith("/")) h = `/${h}`;
  h = h.replace(/\/+$/, "");
  if (basePath && h.startsWith(basePath)) h = h.slice(basePath.length);
  return h.replace(/^\/+/, "");
}

async function listViaPropfind(
  base: string,
  user: string,
  pass: string,
  rel: string,
): Promise<CloudFile[]> {
  const target = davTargetUrl(base, user, rel);
  let basePath = "";
  try {
    basePath = decodeURIComponent(new URL(target).pathname).replace(
      /\/+$/,
      "",
    );
  } catch {
    /* unparseable target — hrefs stay untrimmed */
  }
  let res: Response;
  try {
    res = await fetch(target, {
      method: "PROPFIND",
      headers: {
        Authorization: `Basic ${Buffer.from(`${user}:${pass}`, "utf8").toString("base64")}`,
        Depth: "1",
        "Content-Type": "application/xml",
      },
      body: PROPFIND_BODY,
      cache: "no-store",
      signal: AbortSignal.timeout(PROPFIND_TIMEOUT_MS),
    });
  } catch {
    throw new Error("Cloud nicht erreichbar");
  }
  if (!res.ok) throw new UpstreamError(res.status);

  const xml = await res.text();
  const files: CloudFile[] = [];
  for (const block of xmlAll(xml, "response")) {
    const rawHref = xmlFirst(block, "href");
    if (rawHref === null) continue;
    const href = decodeEntities(rawHref).trim();
    // Directories carry a trailing slash and/or a <collection/> resource type.
    if (href.endsWith("/")) continue;
    const resourcetype = xmlFirst(block, "resourcetype");
    if (resourcetype !== null && /collection/i.test(resourcetype)) continue;
    const relPath = hrefRel(href, basePath);
    if (relPath === "" || relPath === rel) continue; // the requested collection itself
    const lastSeg = relPath.split("/").pop() ?? relPath;
    const displayname = xmlFirst(block, "displayname");
    const length = xmlFirst(block, "getcontentlength");
    const contentType = xmlFirst(block, "getcontenttype");
    const name = (
      displayname !== null ? decodeEntities(displayname) : lastSeg
    ).trim();
    files.push({
      name,
      size:
        length !== null && /^\d+$/.test(length.trim())
          ? Number(length.trim())
          : null,
      mime:
        contentType !== null && contentType.trim() !== ""
          ? decodeEntities(contentType).trim()
          : extMime(name),
      path: relPath,
    });
  }
  return files.sort(byName);
}

/* ── Source 2: Rust bridge (deployed chain — no DAV creds in web env) ─────── */

interface BridgeFileInfo {
  name?: string;
  size?: number | null;
  is_directory?: boolean;
  content_type?: string | null;
}

async function listViaBridge(rel: string): Promise<CloudFile[]> {
  const url = `${backendUrl()}/api/cloud/files/metadata?path=${encodeURIComponent(rel)}`;
  let res: Response;
  try {
    res = await fetch(url, {
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(PROPFIND_TIMEOUT_MS),
    });
  } catch {
    throw new Error("Cloud nicht erreichbar");
  }
  if (!res.ok) throw new UpstreamError(res.status);
  const data = (await res.json()) as { files?: BridgeFileInfo[] };
  const raw = Array.isArray(data.files) ? data.files : [];
  return raw
    .filter(
      (f) =>
        Boolean(f) &&
        f.is_directory !== true &&
        typeof f.name === "string" &&
        f.name !== "",
    )
    .map((f) => ({
      name: f.name as string,
      size: typeof f.size === "number" ? f.size : null,
      mime: f.content_type || extMime(f.name as string),
      path: rel ? `${rel}/${f.name}` : (f.name as string),
    }))
    .sort(byName);
}

/* ── Handler ───────────────────────────────────────────────────────────────── */

export async function GET(request: Request): Promise<Response> {
  const rel = validateRelPath(new URL(request.url).searchParams.get("path"));
  if (rel === null) {
    return Response.json({ error: "Ungültiger Cloud-Pfad" }, { status: 400 });
  }
  const base = resolveDavBase();
  const user = resolveDavUser();
  const pass = resolveDavPass();
  try {
    const files =
      base && user && pass
        ? await listViaPropfind(base, user, pass, rel)
        : await listViaBridge(rel);
    return Response.json(files);
  } catch (err) {
    if (err instanceof UpstreamError) {
      return Response.json(
        { error: `Cloud-Liste fehlgeschlagen (HTTP ${err.status})` },
        { status: 502 },
      );
    }
    const message =
      err instanceof Error && err.message
        ? err.message
        : "Cloud nicht erreichbar";
    return Response.json({ error: message }, { status: 502 });
  }
}
