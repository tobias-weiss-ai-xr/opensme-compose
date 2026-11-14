import type {
  Announcement,
  AiChatRequest,
  AiChatResponse,
  FeedResponse,
  IntercomListResponse,
  IntercomMessage,
} from "./types";

/**
 * Same-origin API client. In the browser these hit the Next.js rewrites
 * (next.config.ts) that proxy /api/* to the Rust backend; on the server
 * (page.tsx) the caller uses API_BASE directly to avoid the rewrite hop.
 */
export const API_BASE =
  typeof window === "undefined"
    ? (process.env.BACKEND_URL || "http://localhost:8080")
    : "";

/**
 * Raised when the Rust backend answers 401 — the surface (intercom/polls)
 * is only available to signed-in users. Callers show the login CTA.
 */
export class AuthError extends Error {
  status = 401;
  constructor(message = "Anmeldung erforderlich") {
    super(message);
    this.name = "AuthError";
  }
}

async function jsonOrAuthError<T>(res: Response, path: string): Promise<T> {
  if (res.status === 401) throw new AuthError();
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  return res.json() as Promise<T>;
}

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    ...init,
  });
  return jsonOrAuthError<T>(res, path);
}

export function fetchServices(): Promise<{ services: import("./types").Service[] }> {
  return getJson("/api/services");
}

export function fetchAnnouncements(): Promise<{ announcements: Announcement[] }> {
  return getJson("/api/announcements");
}

export function fetchFeeds(): Promise<FeedResponse> {
  return getJson<FeedResponse>("/api/feeds").catch(() => ({ columns: [] }));
}

/** Full list (no cursor) or delta `?since=<id>` (newest-first). */
export function fetchIntercom(since?: number): Promise<IntercomListResponse> {
  const qs = since != null && since > 0 ? `?since=${since}` : "";
  return getJson<IntercomListResponse>(`/api/intercom${qs}`);
}

export interface IntercomPayload {
  text: string;
  attachment_url?: string;
}

export async function postIntercom(
  payload: IntercomPayload,
): Promise<{ message: IntercomMessage }> {
  const res = await fetch("/api/intercom", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) {
    let error = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data?.error) error = data.error;
    } catch {
      /* keep HTTP fallback */
    }
    throw new Error(error);
  }
  return res.json() as Promise<{ message: IntercomMessage }>;
}

export interface FanoutChannel {
  ok: boolean;
  detail: string;
}

export interface FanoutResult {
  element: FanoutChannel;
  mail: FanoutChannel;
}

/**
 * Operator broadcast: stores the note via /api/broadcast and fans it out
 * server-side (Element + Stalwart mail). Only available when the operator
 * set INTERCOM_BROADCAST=1 — otherwise the route answers 404 ("HTTP 404").
 */
export async function postBroadcast(text: string): Promise<{
  message: IntercomMessage;
  fanout: FanoutResult;
}> {
  const res = await fetch("/api/broadcast", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) {
    let error = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data?.error) error = data.error;
    } catch {
      /* keep HTTP fallback */
    }
    throw new Error(error);
  }
  return res.json() as Promise<{ message: IntercomMessage; fanout: FanoutResult }>;
}

export async function postIntercomUpload(
  text: string,
  file: File,
): Promise<{ message: IntercomMessage }> {
  const url = `/api/intercom/upload?text=${encodeURIComponent(text)}&filename=${encodeURIComponent(file.name)}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) {
    let error = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data?.error) error = data.error;
    } catch {
      /* keep HTTP fallback */
    }
    throw new Error(error);
  }
  return res.json() as Promise<{ message: IntercomMessage }>;
}

/**
 * Asks the backend to create a public read-only share link for a cloud
 * file (path relative to the user's DAV files root). The returned URL
 * is absolute https — exactly what the intercom attachment validator
 * allowlists; relative cloud paths are rejected there.
 */
export async function postCloudShare(path: string): Promise<{ url: string }> {
  const res = await fetch(`/api/cloud/share?path=${encodeURIComponent(path)}`, {
    method: "POST",
  });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) {
    let error = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data?.error) error = data.error;
    } catch {
      /* keep HTTP fallback */
    }
    throw new Error(error);
  }
  return res.json() as Promise<{ url: string }>;
}

export async function chatWithAi(req: AiChatRequest): Promise<AiChatResponse> {
  const res = await fetch("/api/ai/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<AiChatResponse>;
}
