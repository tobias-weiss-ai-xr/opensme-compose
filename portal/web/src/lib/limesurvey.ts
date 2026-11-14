/**
 * Server-side LimeSurvey client — the portal's Umfragen-Service integration.
 *
 * Speaks the LimeSurvey 6 "RemoteControl" JSON-RPC dialect
 * (POST {method, params, id} to <SURVEYS_URL>/admin/remotecontrol) and keeps
 * the session key for the process lifetime: acquired on first use (single
 * flight), cached, and transparently re-acquired exactly once when
 * LimeSurvey answers `result: {status: "Invalid session key"}`.
 *
 * LimeSurvey quirks baked in here (verified against 6.x):
 * - Method failures arrive as HTTP 200 with `error: null` and a RESULT of
 *   `{status: "…"}` (e.g. wrong password, invalid survey id, expired key) —
 *   never as an RPC error envelope.
 * - `list_surveys` returns an array on current installs, but a JSON-encoded
 *   STRING on older ones — both are accepted.
 * - `get_summary` returns `{}`-like stats or `[]` when the survey has no
 *   responses/tokens tables yet.
 *
 * The ops CLI at scripts/limesurvey-rpc.sh mirrors this contract.
 */

import { serverEnv } from "./config";
import type { SurveySummary } from "./types";

/** One entry of LimeSurvey's `list_surveys` result (subset we consume). */
export interface LimeSurveySurvey {
  sid: number | string;
  surveyls_title?: string;
  active?: string;
  startdate?: string | null;
  expires?: string | null;
}

/** Keys of the `get_summary` result (all optional — subset of interest). */
export interface LimeSurveySummaryStats {
  completed_responses?: number;
  incomplete_responses?: number;
  full_responses?: number;
  token_count?: number;
  token_invalid?: number;
  token_sent?: number;
  token_opted_out?: number;
  token_completed?: number;
  token_screenout?: number;
}

export interface LimeSurveyConfig {
  /** Full JSON-RPC endpoint, e.g. https://surveys.example/admin/remotecontrol */
  rpcUrl: string;
  user: string;
  password: string;
  /** Abort timeout per RPC call (default 8 s). */
  timeoutMs?: number;
  /** Injectable fetch (tests). */
  fetchImpl?: typeof fetch;
}

export class LimeSurveyError extends Error {
  constructor(
    message: string,
    /** The LimeSurvey `result.status` string when that is what failed. */
    readonly lsStatus?: string,
  ) {
    super(message);
    this.name = "LimeSurveyError";
  }
}

interface RpcEnvelope<T> {
  id: number;
  result: T;
  error: string | null;
}

/**
 * LimeSurvey signals method failures via `result: {status: "…"}` with
 * `error: null` and HTTP 200. A genuine stats object never looks like that
 * (several numeric keys), so "an object whose ONLY key is a string status"
 * is a reliable failure marker.
 */
function statusText(result: unknown): string | null {
  if (result && typeof result === "object" && !Array.isArray(result)) {
    const keys = Object.keys(result as Record<string, unknown>);
    if (keys.length === 1 && keys[0] === "status") {
      const s = (result as { status?: unknown }).status;
      if (typeof s === "string") return s;
    }
  }
  return null;
}

export function createLimesurveyClient(config: LimeSurveyConfig) {
  const fetchImpl = config.fetchImpl ?? fetch.bind(globalThis);
  const timeoutMs = config.timeoutMs ?? 8_000;
  let rpcId = 0;
  let sessionKey: string | null = null;
  let sessionRequest: Promise<string> | null = null;

  async function rpc<T>(method: string, params: unknown[]): Promise<T> {
    let res: Response;
    try {
      res = await fetchImpl(config.rpcUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ method, params, id: ++rpcId }),
        cache: "no-store",
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (err) {
      throw new LimeSurveyError(
        `Umfragen-Service nicht erreichbar (${err instanceof Error ? err.message : String(err)})`,
      );
    }
    if (!res.ok) {
      throw new LimeSurveyError(`Umfragen-RPC HTTP ${res.status}`);
    }
    let envelope: RpcEnvelope<T>;
    try {
      envelope = (await res.json()) as RpcEnvelope<T>;
    } catch {
      // Verified LS6 quirk: bad admin credentials answer 200 text/html with
      // an EMPTY body (the controller dies before emitting JSON).
      throw new LimeSurveyError(
        "Umfragen-RPC: ungültige Antwort (leer oder kein JSON — bei der Anmeldung meist falsche Anmeldedaten)",
      );
    }
    if (envelope.error) {
      throw new LimeSurveyError(`Umfragen-RPC: ${envelope.error}`);
    }
    return envelope.result;
  }

  /** Single-flight session-key acquisition — one in-flight request max. */
  function requestSessionKey(): Promise<string> {
    if (!sessionRequest) {
      sessionRequest = rpc<unknown>("get_session_key", [
        config.user,
        config.password,
      ])
        .then((result) => {
          if (typeof result === "string" && result.length > 0) {
            sessionKey = result;
            return result;
          }
          const status = statusText(result);
          throw new LimeSurveyError(
            `Anmeldung am Umfragen-Service fehlgeschlagen (${status ?? "unerwartete Antwort"})`,
            status ?? undefined,
          );
        })
        .finally(() => {
          sessionRequest = null;
        });
    }
    return sessionRequest;
  }

  /** RPC call that leads with the cached session key; one re-auth retry. */
  async function keyedRpc<T>(method: string, params: unknown[]): Promise<T> {
    const key = sessionKey ?? (await requestSessionKey());
    const first = await rpc<unknown>(method, [key, ...params]);
    const status = statusText(first);
    if (status === null) return first as T;
    if (/Invalid session key/i.test(status)) {
      // Cached key expired server-side — re-auth once, then replay the call.
      sessionKey = null;
      const fresh = await requestSessionKey();
      const retry = await rpc<unknown>(method, [fresh, ...params]);
      const retryStatus = statusText(retry);
      if (retryStatus !== null) {
        throw new LimeSurveyError(`Umfragen-RPC: ${retryStatus}`, retryStatus);
      }
      return retry as T;
    }
    throw new LimeSurveyError(`Umfragen-RPC: ${status}`, status);
  }

  /** Accept both the array result (LS6) and the JSON-encoded string (older). */
  function parseSurveyList(result: unknown): LimeSurveySurvey[] {
    let raw: unknown = result;
    if (typeof raw === "string") {
      try {
        raw = JSON.parse(raw) as unknown;
      } catch {
        return [];
      }
    }
    if (!Array.isArray(raw)) return [];
    return (raw as unknown[]).filter(
      (s): s is LimeSurveySurvey =>
        Boolean(s) &&
        typeof s === "object" &&
        (typeof (s as LimeSurveySurvey).sid === "number" ||
          typeof (s as LimeSurveySurvey).sid === "string"),
    );
  }

  function parseSummary(result: unknown): LimeSurveySummaryStats {
    if (result && typeof result === "object" && !Array.isArray(result)) {
      return result as LimeSurveySummaryStats;
    }
    return {}; // e.g. `[]` when no responses/tokens tables exist yet
  }

  /** First numeric present: finished interviews, else invited tokens, else 0. */
  function participantCount(stats: LimeSurveySummaryStats): number {
    const numeric = [
      stats.completed_responses,
      stats.token_count,
      stats.token_completed,
    ].find((v) => typeof v === "number" && Number.isFinite(v));
    return typeof numeric === "number" ? numeric : 0;
  }

  function toSummary(
    s: LimeSurveySurvey,
    stats: LimeSurveySummaryStats,
  ): SurveySummary {
    const id = typeof s.sid === "number" ? s.sid : Number(s.sid);
    return {
      id: Number.isFinite(id) ? id : 0,
      title:
        typeof s.surveyls_title === "string" && s.surveyls_title.trim() !== ""
          ? s.surveyls_title
          : `Umfrage ${s.sid}`,
      status: String(s.active).toUpperCase() === "Y" ? "active" : "closed",
      participants: participantCount(stats),
    };
  }

  async function summaryFor(survey: LimeSurveySurvey): Promise<SurveySummary> {
    const sid =
      typeof survey.sid === "number" ? survey.sid : Number(survey.sid);
    try {
      const stats = parseSummary(
        await keyedRpc<unknown>("get_summary", [Number.isFinite(sid) ? sid : survey.sid]),
      );
      return toSummary(survey, stats);
    } catch {
      // One broken survey must not blank the whole tile.
      return toSummary(survey, {});
    }
  }

  return {
    /** Raw `list_surveys` result, normalized to an array of survey objects. */
    listSurveys: async (): Promise<LimeSurveySurvey[]> =>
      parseSurveyList(await keyedRpc<unknown>("list_surveys", [])),
    /** Raw `get_summary` stats for one survey id. */
    getSummary: async (sid: number): Promise<LimeSurveySummaryStats> =>
      parseSummary(await keyedRpc<unknown>("get_summary", [sid])),
    /** Tile payload: surveys joined with their response/token counts. */
    listSurveysWithParticipants: async (): Promise<SurveySummary[]> => {
      const surveys = parseSurveyList(
        await keyedRpc<unknown>("list_surveys", []),
      );
      return Promise.all(surveys.map(summaryFor));
    },
    /** Best-effort key release (frees a server-side session). */
    releaseSessionKey: async (): Promise<void> => {
      const key = sessionKey;
      if (!key) return;
      sessionKey = null;
      try {
        await rpc<unknown>("release_session_key", [key, config.user]);
      } catch {
        // best effort — nothing sensible to do on failure
      }
    },
  };
}

export type LimeSurveyClient = ReturnType<typeof createLimesurveyClient>;

/* ── Env wiring (server-side, runtime-resolved like the rest of lib/) ─────── */

/** Public Umfragen-Service URL (SURVEYS_URL, domain fallback mirrors config.ts). */
export function surveysUrl(): string {
  const explicit = serverEnv("SURVEYS_URL").trim();
  if (explicit) return explicit.replace(/\/+$/, "");
  const domain =
    serverEnv("OPENSME_DOMAIN", "opensme.graphwiz.ai").trim() ||
    "opensme.graphwiz.ai";
  return `https://surveys.${domain}`;
}

/** JSON-RPC endpoint: SURVEYS_RPC_URL, else <surveys-url>/admin/remotecontrol. */
export function resolveRpcUrl(): string {
  const explicit = serverEnv("SURVEYS_RPC_URL").trim();
  if (explicit) return explicit.replace(/\/+$/, "");
  return `${surveysUrl()}/admin/remotecontrol`;
}

/** True when the operator wired LS admin credentials (RPC available). */
export function surveysConfigured(): boolean {
  return serverEnv("SURVEYS_ADMIN_PASSWORD").trim() !== "";
}

/**
 * Env-driven singleton for the /api/polls route — created once per process
 * so the session key cache survives across requests. Null when no admin
 * password is configured (hermetic/mock mode).
 */
let envClient: LimeSurveyClient | null | undefined;

export function getLimesurveyClient(): LimeSurveyClient | null {
  if (envClient === undefined) {
    const password = serverEnv("SURVEYS_ADMIN_PASSWORD").trim();
    envClient = password
      ? createLimesurveyClient({
          rpcUrl: resolveRpcUrl(),
          user: serverEnv("SURVEYS_ADMIN_USER", "admin").trim() || "admin",
          password,
        })
      : null;
  }
  return envClient;
}
