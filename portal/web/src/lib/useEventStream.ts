"use client";

import { useEffect, useRef, useState } from "react";
import { dispatchEvent, type PortalEvent } from "./events";

/** Test seam: override the global EventSource constructor in unit tests. */
let EventSourceCtor: typeof EventSource | null = null;
export function resetEventSourceForTests() {
  EventSourceCtor = null;
}

const LISTENED_KINDS = [
  "intercom.created", "intercom.deleted",
  "task.created", "task.updated", "task.deleted",
];

/**
 * Open the same-origin SSE stream and forward events to the local dispatcher.
 * `connected` drives the polling fallback: the panels poll only while false.
 * Browsers reconnect automatically with Last-Event-ID; EventSource treats a
 * 204 from the backend (events disabled) as terminal, leaving polling active.
 */
export function useEventStream(authenticated: boolean): { connected: boolean } {
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!authenticated || typeof window === "undefined") return;
    const Ctor = EventSourceCtor ?? window.EventSource;
    if (!Ctor) return;
    const es = new Ctor("/api/events");
    sourceRef.current = es;
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    for (const kind of LISTENED_KINDS) {
      es.addEventListener(kind, (raw) => {
        try {
          const event = JSON.parse((raw as MessageEvent).data) as PortalEvent;
          event.kind = kind; // the server also sends it; keep both paths safe
          dispatchEvent(event);
        } catch { /* ignore malformed frames */ }
      });
    }
    return () => { es.close(); sourceRef.current = null; setConnected(false); };
  }, [authenticated]);

  return { connected };
}
