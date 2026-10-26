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

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    ...init,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  return res.json() as Promise<T>;
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

export function fetchIntercom(): Promise<IntercomListResponse> {
  return getJson<IntercomListResponse>("/api/intercom");
}

export interface IntercomPayload {
  text: string;
  attachment_url?: string;
}

export async function postIntercom(payload: IntercomPayload): Promise<IntercomMessage> {
  const res = await fetch("/api/intercom", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
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
  return res.json() as Promise<IntercomMessage>;
}

export async function postIntercomUpload(
  text: string,
  file: File,
): Promise<IntercomMessage> {
  const url = `/api/intercom/upload?text=${encodeURIComponent(text)}&filename=${encodeURIComponent(file.name)}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
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
  return res.json() as Promise<IntercomMessage>;
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
