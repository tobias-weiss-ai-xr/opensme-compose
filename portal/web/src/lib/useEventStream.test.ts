import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useEventStream, resetEventSourceForTests } from "./useEventStream";
import { dispatchEvent, resetEventsForTests, subscribeEvents } from "./events";

class FakeEventSource {
  static last: FakeEventSource | null = null;
  url: string;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(url: string) { this.url = url; FakeEventSource.last = this; }
  addEventListener(type: string, cb: (e: MessageEvent) => void) {
    if (type === "intercom.created") this.created = cb;
  }
  created: ((e: MessageEvent) => void) | null = null;
  close() {}
  emit(data: unknown) {
    this.created?.(new MessageEvent("intercom.created", { data: JSON.stringify(data) }));
  }
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
    act(() => FakeEventSource.last?.emit({ id: "1-0", kind: "intercom.created", payload: { id: 5 } }));
    expect(received).toHaveLength(1);
  });

  it("does not open a stream for anonymous visitors", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    renderHook(() => useEventStream(false));
    expect(FakeEventSource.last).toBeNull();
  });

  it("reports disconnected on error so the poller resumes", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const { result } = renderHook(() => useEventStream(true));
    act(() => FakeEventSource.last?.onerror?.());
    expect(result.current.connected).toBe(false);
    dispatchEvent({ id: "x", kind: "x", payload: {} }); // no throw
  });
});
