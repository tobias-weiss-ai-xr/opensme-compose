import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useEventStream, resetEventSourceForTests } from "./useEventStream";
import { dispatchEvent, resetEventsForTests, subscribeEvents, SSE_READY_KIND } from "./events";

class FakeEventSource {
  static last: FakeEventSource | null = null;
  url: string;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(url: string) { this.url = url; FakeEventSource.last = this; }
  listeners: Record<string, (e: MessageEvent) => void> = {};
  addEventListener(type: string, cb: (e: MessageEvent) => void) {
    this.listeners[type] = cb;
  }
  close() {}
  emit(type: string, data: unknown) {
    this.listeners[type]?.(new MessageEvent(type, { data: JSON.stringify(data) }));
  }
  ready() { this.emit("ready", {}); }
}

beforeEach(() => { resetEventSourceForTests(); resetEventsForTests(); FakeEventSource.last = null; });
afterEach(() => vi.unstubAllGlobals());

describe("useEventStream", () => {
  it("opens /api/events and dispatches incoming events locally", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const received: unknown[] = [];
    subscribeEvents((e) => received.push(e));
    renderHook(() => useEventStream(true));
    expect(FakeEventSource.last?.url).toBe("/api/events");
    act(() => FakeEventSource.last?.emit("intercom.created", { id: "1-0", kind: "intercom.created", payload: { id: 5 } }));
    expect(received).toHaveLength(1);
  });

  it("forwards poll.created and poll.voted frames to subscribers", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const kinds: string[] = [];
    subscribeEvents((e) => kinds.push(e.kind));
    renderHook(() => useEventStream(true));
    act(() => FakeEventSource.last?.emit("poll.created", {
      id: "1-0", kind: "poll.created", payload: { poll: { id: 7 } },
    }));
    act(() => FakeEventSource.last?.emit("poll.voted", {
      id: "1-1", kind: "poll.voted", payload: { poll: { id: 7 } },
    }));
    expect(kinds).toEqual(["poll.created", "poll.voted"]);
  });

  it("does not open a stream for anonymous visitors", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    renderHook(() => useEventStream(false));
    expect(FakeEventSource.last).toBeNull();
  });

  it("emits a local ready signal so panels reconcile with the authoritative store", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const kinds: string[] = [];
    subscribeEvents((e) => kinds.push(e.kind));
    renderHook(() => useEventStream(true));
    act(() => FakeEventSource.last?.ready());
    expect(kinds).toContain(SSE_READY_KIND);
  });

  it("keeps polling until the reader is ready, then pauses; errors resume polling", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const { result } = renderHook(() => useEventStream(true));
    expect(result.current.connected).toBe(false);
    act(() => FakeEventSource.last?.onopen?.());
    expect(result.current.connected).toBe(false); // headers alone are not enough
    act(() => FakeEventSource.last?.ready());
    expect(result.current.connected).toBe(true);
    act(() => FakeEventSource.last?.onerror?.());
    expect(result.current.connected).toBe(false);
    dispatchEvent({ id: "x", kind: "x", payload: {} }); // no throw
  });
});
