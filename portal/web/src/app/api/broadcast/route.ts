import { NextResponse } from "next/server";
import { serverEnv } from "@/lib/config";
import { backendUrl, forwardToBackend } from "@/lib/backend";
import type { IntercomMessage } from "@/lib/types";

/**
 * POST /api/broadcast — operator broadcast fanout (INTERCOM_BROADCAST=1).
 *
 * Dedicated route, takes precedence over the [[...path]] catch-all.
 * Server-side fanout for the IntercomPanel broadcast box:
 *   1. store the note in the intercom backend (same POST /api/intercom
 *      hop the client would take, with the caller's session cookie),
 *   2. publish a `broadcast.requested` event to the protected backend stream;
 *      the portal-web instrumentation worker consumes it and performs the
 *      Element/Mail fanout asynchronously (worker lifecycle is separate).
 *
 * The feature is invisible unless the operator enables it: any request
 * with INTERCOM_BROADCAST unset (or falsy) is answered 404 — "404 hides
 * backend surface", same stance as the catch-all's sanitiser.
 */

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BROADCAST_MAX_LEN = 2000; // mirrors the IntercomPanel textarea cap

export async function POST(req: Request): Promise<Response> {
  const flag = serverEnv("INTERCOM_BROADCAST");
  if (flag !== "1" && flag !== "true") {
    return NextResponse.json({ error: "Not Found" }, { status: 404 });
  }

  const body = (await req.json().catch(() => null)) as { text?: unknown } | null;
  const text = typeof body?.text === "string" ? body.text.trim() : "";
  if (!text) {
    return NextResponse.json(
      { error: "text fehlt — der Broadcast darf nicht leer sein" },
      { status: 400 },
    );
  }
  if (text.length > BROADCAST_MAX_LEN) {
    return NextResponse.json(
      { error: `text zu lang — max. ${BROADCAST_MAX_LEN} Zeichen` },
      { status: 400 },
    );
  }

  // Leg 1: store in the intercom backend with the caller's session.
  const headers = new Headers();
  const contentType = req.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const cookie = req.headers.get("cookie");
  if (cookie) headers.set("cookie", cookie);

  let upstream;
  try {
    upstream = await forwardToBackend({
      url: `${backendUrl()}/api/intercom`,
      method: "POST",
      headers,
      body: JSON.stringify({ text }),
      timeoutMs: 15_000,
    });
  } catch {
    return NextResponse.json({ error: "portal backend unreachable" }, { status: 502 });
  }

  if (upstream.status === 401) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  if (upstream.status < 200 || upstream.status >= 300) {
    let detail = `HTTP ${upstream.status}`;
    try {
      const data = JSON.parse(new TextDecoder().decode(upstream.bytes)) as { error?: string };
      if (data?.error) detail = data.error;
    } catch {
      /* keep the HTTP fallback */
    }
    return NextResponse.json({ error: detail }, { status: upstream.status });
  }

  let message: IntercomMessage;
  try {
    const data = JSON.parse(new TextDecoder().decode(upstream.bytes)) as {
      message?: IntercomMessage;
    };
    if (!data?.message) throw new Error("no message");
    message = data.message;
  } catch {
    return NextResponse.json(
      { error: "intercom store returned no message" },
      { status: 502 },
    );
  }

  // Leg 2: queue the fanout on the event stream. The note is already stored;
  // the consumer worker performs Element + mail delivery asynchronously.
  const queuedHeaders: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const sessionCookie = req.headers.get("cookie");
  if (sessionCookie) queuedHeaders.Cookie = sessionCookie;
  const queued = await forwardToBackend({
    url: `${backendUrl()}/api/events/publish`,
    method: "POST",
    headers: queuedHeaders,
    body: JSON.stringify({ kind: "broadcast.requested", text }),
    timeoutMs: 10_000,
  });
  if (queued.status < 200 || queued.status >= 300) {
    return NextResponse.json(
      { message, queued: false, error: `queue failed (HTTP ${queued.status})` },
      { status: 502 },
    );
  }
  return NextResponse.json({ message, queued: true });
}
