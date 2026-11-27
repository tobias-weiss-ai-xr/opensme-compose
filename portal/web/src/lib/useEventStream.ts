"use client";

import { useEffect, useRef, useState } from "react";
import { dispatchEvent, SSE_READY_KIND, type PortalEvent } from "./events";

/** Test seam: override the global EventSource constructor in unit tests. */
let EventSourceCtor: typeof EventSource | null = null;
export function resetEventSourceForTests() {
  EventSourceCtor = null;
}

const LAST_ID_KEY = "opensme:events:last-id";
const SEEN_KEY = "opensme:events:seen";
const MAX_SEEN_IDS = 512;

const LISTENED_KINDS = [
  "intercom.created", "intercom.deleted",
  "task.created", "task.updated", "task.deleted",
  "poll.created", "poll.voted",
];

function readSeen(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.sessionStorage.getItem(SEEN_KEY);
    const ids = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(
      Array.isArray(ids)
        ? ids.filter((id): id is string => typeof id === "string")
        : [],
    );
  } catch {
    return new Set();
  }
}

function remember(id: string, seen: Set<string>): void {
  seen.add(id);
  while (seen.size > MAX_SEEN_IDS) {
    const oldest = seen.values().next().value;
    if (typeof oldest !== "string") break;
    seen.delete(oldest);
  }
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(SEEN_KEY, JSON.stringify([...seen]));
    window.sessionStorage.setItem(LAST_ID_KEY, id);
  } catch {
    // Storage may be disabled; in-memory dedupe still works for this tab.
  }
}

/**
 * Open the same-origin SSE stream, resuming from the last seen stream ID after
 * reload. Native EventSource reconnect supplies Last-Event-ID automatically;
 * the `?since=` query covers fresh mounts in the same tab.
 */
export function useEventStream(authenticated: boolean): { connected: boolean } {
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!authenticated || typeof window === "undefined") return;
    const Ctor = EventSourceCtor ?? window.EventSource;
    if (!Ctor) return;
    let since = "";
    try {
      since = window.sessionStorage.getItem(LAST_ID_KEY) ?? "";
    } catch {
      // Storage may be disabled; reconnect still uses Last-Event-ID.
    }
    const seen = readSeen();
    const es = new Ctor(
      since ? `/api/events?since=${encodeURIComponent(since)}` : "/api/events",
    );
    sourceRef.current = es;
    es.onerror = () => setConnected(false);
    es.addEventListener("ready", () => {
      setConnected(true);
      dispatchEvent({ id: "sse-ready", kind: SSE_READY_KIND, payload: {} });
    });
    for (const kind of LISTENED_KINDS) {
      es.addEventListener(kind, (raw) => {
        try {
          const msg = raw as MessageEvent;
          const event = JSON.parse(msg.data) as PortalEvent;
          event.kind = kind;
          const id = msg.lastEventId || event.id;
          if (id && seen.has(id)) return;
          if (id) remember(id, seen);
          dispatchEvent(event);
        } catch {
          // Ignore malformed frames.
        }
      });
    }
    return () => {
      es.close();
      sourceRef.current = null;
      setConnected(false);
    };
  }, [authenticated]);

  return { connected };
}
