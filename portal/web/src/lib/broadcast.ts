import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import type { FanoutChannelResult, FanoutReport } from "@/lib/types";

/**
 * Server-side broadcast fanout (Element + Stalwart mail).
 *
 * The route handler (app/api/broadcast/route.ts) stores the note in the
 * intercom backend and then hands the text to this module:
 *
 *   1. Element leg: Matrix client-v3 login → resolve the support room
 *      ('!id' direct or '#alias' via the directory) → PUT send
 *      m.room.message with an idempotent txn id. Same flow as
 *      scripts/booking-notify.sh (T2), but in TypeScript — the shell
 *      script stays the operator-facing loop tool, this is the portal's
 *      on-demand leg.
 *   2. Mail leg: execFile 'bash scripts/broadcast-fanout.sh' — the
 *      portal-web image (Next standalone, node:22-alpine, read-only
 *      rootfs) ships neither python3 nor an SMTP stack, so the SMTP send
 *      happens in the repo-root script (python3 smtplib against the
 *      stalwart listener on 127.0.0.1).
 *
 * Both legs are best-effort: a failure on one channel never loses the
 * note (it is already stored) and never fails the route — the per-channel
 * outcome travels back in the response for the UI status line.
 *
 * Secrets discipline: the Matrix password and access token NEVER appear
 * in errors or details (only statuses/room ids/event ids); the mail leg
 * carries no secrets at all (plain port 25 on the compose-internal
 * interface).
 */

const MATRIX_TIMEOUT_MS = 10_000;
/** One login retry on 429 — capped far below the shell script's 60s so the HTTP request stays snappy. */
const RETRY_CAP_MS = 5_000;
const SCRIPT_TIMEOUT_MS = 60_000;
const ANSI_RE = /\x1b\[[0-9;]*m/g;
/** Leading status tag of the shell helpers' colored output. */
const TAG_RE = /^\[(?:OK|ERR|INFO|WARN)\]\s*/;

export interface BroadcastConfig {
  matrixHomeserver: string;
  matrixUser: string;
  matrixPassword: string;
  matrixRoom: string;
  /** comma-separated recipients; empty → the fanout script discovers from bootstrap/demo-users.json */
  mailTo: string;
  smtpHost: string;
  smtpPort: string;
  mailFrom: string;
  subject: string;
  fanoutScript: string;
}

/**
 * Default fanout-script path: walk upward from cwd. `next dev` runs with
 * cwd <repo>/portal/web, the standalone server with /app — in a checkout
 * the script lives at <repo>/scripts/broadcast-fanout.sh (1–2 levels up).
 * In the portal-web image the script is absent by design: the operator
 * mounts it and points BROADCAST_FANOUT_SCRIPT at it.
 */
function findFanoutScript(start: string): string {
  let dir = path.resolve(start);
  for (let depth = 0; depth < 5; depth += 1) {
    const candidate = path.join(dir, "scripts", "broadcast-fanout.sh");
    if (existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return path.resolve(start, "scripts", "broadcast-fanout.sh");
}

/** Env wiring with the same defaults as scripts/booking-notify.sh / broadcast-fanout.sh. */
export function broadcastConfig(
  env: Record<string, string | undefined> = process.env,
): BroadcastConfig {
  const domain = env.MATRIX_DOMAIN || "matrix.home.opensme.graphwiz.ai";
  return {
    matrixHomeserver: env.MATRIX_HOMESERVER || `https://${domain}`,
    matrixUser: env.MATRIX_USER || "testuser",
    matrixPassword: env.MATRIX_PASSWORD || env.E2E_TESTUSER_PASSWORD || "eval2026",
    matrixRoom: env.MATRIX_ROOM || `#support:${domain}`,
    mailTo: env.BROADCAST_MAIL_TO || "",
    smtpHost: env.BROADCAST_SMTP_HOST || "127.0.0.1",
    smtpPort: env.BROADCAST_SMTP_PORT || "25",
    mailFrom: env.BROADCAST_MAIL_FROM || "",
    subject: env.BROADCAST_SUBJECT || "[openSME] Broadcast an alle",
    fanoutScript: env.BROADCAST_FANOUT_SCRIPT || findFanoutScript(process.cwd()),
  };
}

/** Strip ANSI colors the shell helpers always emit (piped stdout included). */
function clean(text: string): string {
  return text.replace(ANSI_RE, "").trim();
}

function lastLine(text: string): string {
  const lines = clean(text).split("\n").filter((l) => l.length > 0);
  const line = lines.length > 0 ? lines[lines.length - 1] : "";
  return line.replace(TAG_RE, "");
}

/**
 * Matrix client-v3 request returning {status, body} — never throws on
 * HTTP errors (transport errors throw and are caught by the caller).
 */
async function mxReq(
  homeserver: string,
  method: "GET" | "POST" | "PUT",
  apiPath: string,
  body: unknown,
  accessToken?: string,
): Promise<{ status: number; json: Record<string, unknown> | null }> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
  const res = await fetch(`${homeserver}${apiPath}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    signal: AbortSignal.timeout(MATRIX_TIMEOUT_MS),
  });
  let json: Record<string, unknown> | null = null;
  try {
    json = (await res.json()) as Record<string, unknown>;
  } catch {
    /* non-JSON body — status is enough */
  }
  return { status: res.status, json };
}

/** Password-holder login with a single 429 retry (retry_after_ms honored, capped). */
async function mxLogin(cfg: BroadcastConfig): Promise<string> {
  const loginBody = {
    type: "m.login.password",
    identifier: { type: "m.id.user", user: cfg.matrixUser },
    password: cfg.matrixPassword,
  };
  let res = await mxReq(cfg.matrixHomeserver, "POST", "/_matrix/client/v3/login", loginBody);
  if (res.status === 429) {
    const wait = Math.min(
      RETRY_CAP_MS,
      typeof res.json?.retry_after_ms === "number" ? res.json.retry_after_ms : RETRY_CAP_MS,
    );
    await new Promise((r) => setTimeout(r, wait));
    res = await mxReq(cfg.matrixHomeserver, "POST", "/_matrix/client/v3/login", loginBody);
  }
  const token = res.json?.access_token;
  if (res.status !== 200 || typeof token !== "string" || token.length === 0) {
    throw new Error(`matrix login failed (HTTP ${res.status})`);
  }
  return token;
}

/** Resolve the target room: '!room_id' passes through, '#alias' via the directory. */
async function mxResolveRoom(
  cfg: BroadcastConfig,
  token: string,
): Promise<string> {
  const room = cfg.matrixRoom;
  if (room.startsWith("!")) return room;
  const res = await mxReq(
    cfg.matrixHomeserver,
    "GET",
    `/_matrix/client/v3/directory/room/${encodeURIComponent(room)}`,
    undefined,
    token,
  );
  const roomId = res.json?.room_id;
  if (res.status !== 200 || typeof roomId !== "string" || roomId.length === 0) {
    throw new Error(`room resolve failed (HTTP ${res.status})`);
  }
  return roomId;
}

/** Element leg: login → resolve → idempotent send. Returns a UI-safe result. */
export async function matrixBroadcast(
  text: string,
  cfg: BroadcastConfig = broadcastConfig(),
): Promise<FanoutChannelResult> {
  try {
    const token = await mxLogin(cfg);
    const roomId = await mxResolveRoom(cfg, token);
    const txnId = `broadcast-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const res = await mxReq(
      cfg.matrixHomeserver,
      "PUT",
      `/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/${encodeURIComponent(txnId)}`,
      { msgtype: "m.text", body: text },
      token,
    );
    const eventId = res.json?.event_id;
    if (res.status !== 200 || typeof eventId !== "string" || eventId.length === 0) {
      return { ok: false, detail: `send failed (HTTP ${res.status})` };
    }
    return { ok: true, detail: `event ${eventId} → ${roomId}` };
  } catch (err) {
    // transport-level failure — message contains no secrets (URLs/status only)
    return {
      ok: false,
      detail: err instanceof Error ? err.message : String(err),
    };
  }
}

/** Mail leg: execFile the repo-root fanout script (python3 smtplib lives there). */
export async function mailBroadcast(
  text: string,
  cfg: BroadcastConfig = broadcastConfig(),
): Promise<FanoutChannelResult> {
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    BROADCAST_SUBJECT: cfg.subject,
    BROADCAST_BODY: text,
    BROADCAST_SMTP_HOST: cfg.smtpHost,
    BROADCAST_SMTP_PORT: cfg.smtpPort,
  };
  if (cfg.mailTo) env.BROADCAST_MAIL_TO = cfg.mailTo;
  if (cfg.mailFrom) env.BROADCAST_MAIL_FROM = cfg.mailFrom;
  return new Promise<FanoutChannelResult>((resolve) => {
    execFile(
      "bash",
      [cfg.fanoutScript],
      { env, timeout: SCRIPT_TIMEOUT_MS },
      (err, stdout, stderr) => {
        if (!err) {
          resolve({ ok: true, detail: lastLine(String(stdout)) || "delivered" });
          return;
        }
        const code = (err as NodeJS.ErrnoException & { code?: number | string }).code;
        const why = lastLine(String(stderr)) || lastLine(String(stdout)) || err.message;
        resolve({ ok: false, detail: `exit ${code ?? "signal"} — ${why}` });
      },
    );
  });
}

/** Both legs in parallel, best-effort — the note is already stored when this runs. */
export async function fanoutBroadcast(
  text: string,
  cfg: BroadcastConfig = broadcastConfig(),
): Promise<FanoutReport> {
  const [element, mail] = await Promise.all([
    matrixBroadcast(text, cfg),
    mailBroadcast(text, cfg),
  ]);
  return { element, mail };
}
