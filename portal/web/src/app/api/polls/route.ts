import { backendUrl, forwardToBackend } from "@/lib/backend";
import { getLimesurveyClient, surveysUrl } from "@/lib/limesurvey";
import { probeSession } from "@/lib/session";
import type { SurveySummary } from "@/lib/types";

/**
 * GET /api/polls — Umfragen tile data, two modes:
 *
 * 1. SURVEYS_ADMIN_PASSWORD set (wired mode): the LimeSurvey JSON-RPC client
 *    (lib/limesurvey.ts) serves the normalized survey list
 *    `{source:"limesurvey", surveys_url, surveys:[{id,title,status,participants}]}`.
 *    Anonymous callers get 401 — the tile is a logged-in feature, mirroring
 *    the Rust backend's session gate.
 *
 * 2. No credentials configured (hermetic dev/test mode): the request is first
 *    forwarded to the Rust backend's team-poll list so a deployed stack keeps
 *    serving real polls verbatim; only when the backend has no polls feature
 *    (or is down) does this route answer with hermetic German mock surveys —
 *    the tile stays alive without any backing services.
 *
 * POST /api/polls — team-poll creation. A concrete app route takes precedence
 * over the catch-all [[...path]] proxy for exactly this path, so creation is
 * forwarded to the Rust backend verbatim (vote/upload endpoints live on
 * sibling paths and still hit the proxy untouched).
 *
 * Ops: the RPC endpoint must be enabled in LimeSurvey once
 * (`scripts/limesurvey-rpc.sh enable-rpc`) and the portal-web container needs
 * SURVEYS_ADMIN_USER/SURVEYS_ADMIN_PASSWORD in its environment — see
 * docs/af-int-T5-polls.txt.
 */

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BACKEND_TIMEOUT_MS = 5_000;

/** Hermetic dev/test fallback (never served when LS credentials are wired). */
const MOCK_SURVEYS: SurveySummary[] = [
  { id: 100001, title: "Team-Feedback 2026", status: "active", participants: 12 },
  {
    id: 100002,
    title: "Schulung: Einführung in die openSME-Cloud",
    status: "active",
    participants: 8,
  },
  {
    id: 100003,
    title: "Betriebsausflug — Terminabstimmung",
    status: "closed",
    participants: 23,
  },
];

function cookieHeader(request: Request): string {
  return request.headers.get("cookie") ?? "";
}

export async function GET(request: Request): Promise<Response> {
  const client = getLimesurveyClient();
  if (!client) {
    // Hermetic mode — real backend polls win, mock surveys fill the gap.
    try {
      const cookie = cookieHeader(request);
      const upstream = await forwardToBackend({
        url: `${backendUrl()}/api/polls`,
        method: "GET",
        headers: cookie ? { cookie } : undefined,
        timeoutMs: BACKEND_TIMEOUT_MS,
      });
      if (upstream.status === 200) {
        return new Response(upstream.bytes, {
          status: 200,
          headers: upstream.headers,
        });
      }
    } catch {
      // backend unreachable → mock fallback below
    }
    return Response.json({
      source: "mock",
      surveys_url: surveysUrl(),
      surveys: MOCK_SURVEYS,
    });
  }

  // Wired mode — a portal session is required before RPC data leaves the box.
  const authed = await probeSession(backendUrl(), cookieHeader(request));
  if (!authed) {
    return Response.json({ error: "Anmeldung erforderlich" }, { status: 401 });
  }
  try {
    const surveys = await client.listSurveysWithParticipants();
    return Response.json({
      source: "limesurvey",
      surveys_url: surveysUrl(),
      surveys,
    });
  } catch (err) {
    const detail = err instanceof Error && err.message ? ` (${err.message})` : "";
    return Response.json(
      { error: `Umfragen-Service nicht erreichbar${detail}` },
      { status: 502 },
    );
  }
}

export async function POST(request: Request): Promise<Response> {
  // Route shadowing compensation: hand team-poll creation to the Rust backend
  // exactly like the catch-all proxy would have.
  try {
    const upstream = await forwardToBackend({
      url: `${backendUrl()}/api/polls`,
      method: "POST",
      headers: {
        "content-type":
          request.headers.get("content-type") ?? "application/json",
        ...(cookieHeader(request) ? { cookie: cookieHeader(request) } : {}),
      },
      body: await request.arrayBuffer(),
      timeoutMs: BACKEND_TIMEOUT_MS,
    });
    return new Response(upstream.bytes, {
      status: upstream.status,
      headers: upstream.headers,
    });
  } catch {
    return Response.json(
      { error: "Backend nicht erreichbar" },
      { status: 502 },
    );
  }
}
