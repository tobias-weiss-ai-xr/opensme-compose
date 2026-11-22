import { describe, it, expect, vi, beforeEach } from "vitest";
import { dispatchEvent, resetEventsForTests, subscribeEvents } from "./events";

beforeEach(() => resetEventsForTests());

describe("local event dispatcher", () => {
  it("delivers only to subscribed handlers and unsubscribes cleanly", () => {
    const a = vi.fn();
    const b = vi.fn();
    const off = subscribeEvents(a);
    subscribeEvents(b);
    dispatchEvent({ id: "1-0", kind: "intercom.created", payload: {} });
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
    off();
    dispatchEvent({ id: "2-0", kind: "intercom.created", payload: {} });
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(2);
  });
});
