import { backendUrl } from "@/lib/backend";
import {
  davAuthHeader,
  extMime,
  listDir,
  UpstreamError,
  validateRelPath,
  webdavRoot,
  type CloudFile,
} from "@/lib/clouddav";

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
 *    The shared DAV logic lives in lib/clouddav.ts (also used by
 *    POST /api/ai/docs for the KI-Analyse file download).
 * 2. Rust bridge `GET {BACKEND_URL}/api/cloud/files/metadata` — the
 *    deployed compose chain gives portal-web no DAV secrets, only the
 *    backend URL, so the listing is delegated to the portal service.
 *
 * The returned `path` is relative to the user's DAV files root —
 * exactly what POST /api/cloud/share (share-link creation) expects.
 */

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/* ── Source 2: Rust bridge (deployed chain — no DAV creds in web env) ─────── */

interface BridgeFileInfo {
  name?: string;
  size?: number | null;
  is_directory?: boolean;
  content_type?: string | null;
}

const BRIDGE_TIMEOUT_MS = 15_000;

async function listViaBridge(rel: string, cookieHeader: string | null): Promise<CloudFile[]> {
  const url = `${backendUrl()}/api/cloud/files/metadata?path=${encodeURIComponent(rel)}`;
  let res: Response;
  try {
    res = await fetch(url, {
      cache: "no-store",
      // The Rust bridge sits behind the portal's session firewall — the
      // signed-in caller's session cookie must be forwarded or every
      // listing answers 401 (found live 2026-11).
      headers: cookieHeader
        ? { Accept: "application/json", Cookie: cookieHeader }
        : { Accept: "application/json" },
      signal: AbortSignal.timeout(BRIDGE_TIMEOUT_MS),
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
    .sort((a, b) => a.name.localeCompare(b.name, "de", {
      sensitivity: "base",
      numeric: true,
    }));
}

/* ── Handler ───────────────────────────────────────────────────────────────── */

export async function GET(request: Request): Promise<Response> {
  const rel = validateRelPath(new URL(request.url).searchParams.get("path"));
  if (rel === null) {
    return Response.json({ error: "Ungültiger Cloud-Pfad" }, { status: 400 });
  }
  try {
    const cookieHeader = request.headers.get("cookie");
    const files =
      webdavRoot() && davAuthHeader()
        ? await listDir(rel)
        : await listViaBridge(rel, cookieHeader);
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
