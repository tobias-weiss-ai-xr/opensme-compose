/**
 * Shared WebDAV client for the OpenCloud integration surfaces — the
 * logic factored out of the /api/cloud/list route so the AI doc-analysis
 * route (/api/ai/docs) reuses the exact same env resolution, auth and
 * path handling (single source of truth for the DAV contract).
 *
 * Environment resolution (first hit wins):
 *   root: OPENCLOUD_WEBDAV_URL ?? CLOUD_WEBDAV_URL
 *         ?? OPENCLOUD_URL + /remote.php/dav/files
 *         ?? https://OPENCLOUD_DOMAIN + /remote.php/dav/files
 *   user: OPENCLOUD_ADMIN_USER ?? CLOUD_WEBDAV_USER
 *   pass: OPENCLOUD_ADMIN_PASSWORD ?? OPENCLOUD_ADMIN_PASS ?? CLOUD_WEBDAV_PASS
 *
 * Every failure is thrown as either a DavUnavailableError (nothing
 * configured / cloud unreachable), an UpstreamError (HTTP != 2xx) or a
 * plain Error with a user-facing German message — the routes map them
 * to JSON error payloads.
 *
 * Framework-free (no next/* imports) so the whole module is directly
 * unit-testable with a stubbed global fetch.
 */

export interface CloudFile {
  name: string;
  size: number | null;
  mime: string;
  path: string;
}

/** Result of downloadFile() — `text` is null for non-text mimes. */
export interface CloudFileDownload {
  name: string;
  mime: string;
  text: string | null;
}

/** Upstream answered with a non-2xx status — surfaced as a 502. */
export class UpstreamError extends Error {
  constructor(readonly status: number) {
    super(`upstream HTTP ${status}`);
  }
}

/** WebDAV root/credentials not configured — the caller can fall back. */
export class DavUnavailableError extends Error {
  constructor() {
    super("Cloud nicht erreichbar");
  }
}

const DAV_TIMEOUT_MS = 15_000;

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

export function extMime(name: string): string {
  const m = /\.([^.]+)$/.exec(name);
  return (m && EXT_MIME[m[1].toLowerCase()]) || "application/octet-stream";
}

function env(name: string): string | undefined {
  const v = process.env[name];
  return v && v.trim() !== "" ? v.trim() : undefined;
}

/** WebDAV files root, e.g. https://cloud.example/remote.php/dav/files */
export function webdavRoot(): string | null {
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

function davUser(): string | undefined {
  return env("OPENCLOUD_ADMIN_USER") ?? env("CLOUD_WEBDAV_USER");
}

function davPass(): string | undefined {
  return (
    env("OPENCLOUD_ADMIN_PASSWORD") ??
    env("OPENCLOUD_ADMIN_PASS") ??
    env("CLOUD_WEBDAV_PASS")
  );
}

/** Precomputed Basic Authorization header value, or null without creds. */
export function davAuthHeader(): string | null {
  const user = davUser();
  const pass = davPass();
  if (!user || !pass) return null;
  return `Basic ${Buffer.from(`${user}:${pass}`, "utf8").toString("base64")}`;
}

/** "" (root) is valid; leading/trailing slashes and ./.. segments are not. */
export function validateRelPath(raw: string | null): string | null {
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

/* ── URL/path helpers ──────────────────────────────────────────────────────── */

function davTargetUrl(base: string, user: string, rel: string): string {
  const cleanBase = base.replace(/\/+$/, "");
  const parts = rel
    ? rel
        .split("/")
        .map(encodeURIComponent)
        .join("/")
    : "";
  // Trailing slash on non-root targets — PROPFIND collection convention.
  return `${cleanBase}/${encodeURIComponent(user)}/${parts ? `${parts}/` : ""}`;
}

/** GET target for a single file — no trailing slash (that's a collection). */
function davFileUrl(base: string, user: string, rel: string): string {
  const parts = rel.split("/").map(encodeURIComponent).join("/");
  return `${base.replace(/\/+$/, "")}/${encodeURIComponent(user)}/${parts}`;
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

/** Last path segment, percent-decoded when well-formed (display name). */
function decodeName(rel: string): string {
  const last = rel.split("/").pop() ?? rel;
  try {
    return decodeURIComponent(last);
  } catch {
    return last; // malformed escape — keep raw
  }
}

/* ── Shared request plumbing ───────────────────────────────────────────────── */

interface DavConfig {
  root: string;
  user: string;
  auth: string;
}

/** Resolve config or throw DavUnavailableError — the "no secrets" case. */
function davConfig(): DavConfig {
  const root = webdavRoot();
  const user = davUser();
  const auth = davAuthHeader();
  if (!root || !user || !auth) throw new DavUnavailableError();
  return { root, user, auth };
}

async function davFetch(url: string, auth: string, init: RequestInit): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      headers: { Authorization: auth, ...(init.headers as Record<string, string>) },
      cache: "no-store",
      signal: AbortSignal.timeout(DAV_TIMEOUT_MS),
    });
  } catch {
    throw new Error("Cloud nicht erreichbar");
  }
  if (!res.ok) throw new UpstreamError(res.status);
  return res;
}

/* ── PROPFIND listing (Depth: 1, files only) ───────────────────────────────── */

export async function listDir(rel: string): Promise<CloudFile[]> {
  const { root, user, auth } = davConfig();
  const clean = rel.replace(/^\/+/, "").replace(/\/+$/, "");
  const target = davTargetUrl(root, user, clean);
  let basePath = "";
  try {
    basePath = decodeURIComponent(new URL(target).pathname).replace(/\/+$/, "");
  } catch {
    /* unparseable target — hrefs stay untrimmed */
  }

  const res = await davFetch(target, auth, {
    method: "PROPFIND",
    headers: { Depth: "1", "Content-Type": "application/xml" },
    body: PROPFIND_BODY,
  });

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
    if (relPath === "" || relPath === clean) continue; // the requested collection itself
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

/* ── File download (GET) with text extraction ──────────────────────────────── */

const TEXT_MIME_EXACT = new Set([
  "application/json",
  "application/xml",
  "application/javascript",
  "application/x-yaml",
  "application/yaml",
  "application/toml",
]);

/** Text mimes we read as strings — everything else (binaries) → text:null. */
function isTextMime(mime: string): boolean {
  const m = mime.toLowerCase();
  return (
    m.startsWith("text/") ||
    TEXT_MIME_EXACT.has(m) ||
    m.endsWith("+xml") ||
    m.endsWith("+json")
  );
}

/**
 * GET one file from the user's DAV files root. Text-ish mimes are read
 * as strings; binaries (images, PDFs, office files …) yield `text:null`
 * so the caller can decide (the AI route answers 422 unsupported).
 */
export async function downloadFile(rel: string): Promise<CloudFileDownload> {
  const { root, user, auth } = davConfig();
  const clean = rel.replace(/^\/+/, "").replace(/\/+$/, "");
  if (clean === "") throw new Error("Ungültiger Cloud-Pfad");
  const url = davFileUrl(root, user, clean);
  const name = decodeName(clean);
  const res = await davFetch(url, auth, {
    method: "GET",
    headers: { Accept: "*/*" },
  });
  const mime =
    (res.headers.get("content-type") ?? "").split(";")[0].trim() ||
    extMime(name);
  const text = isTextMime(mime) ? await res.text() : null;
  return { name, mime, text };
}
