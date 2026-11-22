/** Client-side event bus fed by the SSE hook; panels subscribe without
 *  importing each other (same isolation principle as lib/bridge.ts). */
export interface PortalEvent {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
}

type Handler = (event: PortalEvent) => void;
const handlers = new Set<Handler>();

export function subscribeEvents(handler: Handler): () => void {
  handlers.add(handler);
  return () => { handlers.delete(handler); };
}

export function dispatchEvent(event: PortalEvent): void {
  for (const handler of handlers) handler(event);
}

export function resetEventsForTests(): void {
  handlers.clear();
}
