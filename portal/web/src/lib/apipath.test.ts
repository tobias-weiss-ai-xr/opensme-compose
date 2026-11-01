import { describe, expect, it } from "vitest";
import { sanitizeApiPath } from "./apipath";

describe("sanitizeApiPath", () => {
  it("passes ordinary api segments through", () => {
    expect(sanitizeApiPath(["session"])).toEqual(["session"]);
    expect(sanitizeApiPath(["services"])).toEqual(["services"]);
    expect(sanitizeApiPath(["intercom", "messages"])).toEqual([
      "intercom",
      "messages",
    ]);
  });

  it("rejects decoded traversal segments (the /api/..%2f..%2fmetrics bypass)", () => {
    expect(sanitizeApiPath(["..", "..", "metrics"])).toBeNull();
    expect(sanitizeApiPath([".."])).toBeNull();
    expect(sanitizeApiPath(["a", "..", "b"])).toBeNull();
  });

  it("rejects dot segments", () => {
    expect(sanitizeApiPath(["."])).toBeNull();
    expect(sanitizeApiPath(["a", ".", "b"])).toBeNull();
  });

  it("rejects double-encoded segments that would decode server-side", () => {
    expect(sanitizeApiPath(["%2e%2e", "%2e%2e", "metrics"])).toBeNull();
    expect(sanitizeApiPath(["a%2fb"])).toBeNull();
    expect(sanitizeApiPath(["..%2f"])).toBeNull();
  });

  it("rejects a decoded-slash segment (the %2f %2f smuggling form)", () => {
    // Next splits on literal '/' FIRST, then decodes each segment: so
    // /api/..%2f..%2fmetrics arrives as the single segment "../../metrics".
    expect(sanitizeApiPath(["../../metrics"])).toBeNull();
    expect(sanitizeApiPath(["..", "..%2fmetrics"])).toBeNull();
    expect(sanitizeApiPath(["metrics/.."])).toBeNull();
  });

  it("handles missing/empty params", () => {
    expect(sanitizeApiPath(undefined)).toEqual([]);
    expect(sanitizeApiPath([])).toEqual([]);
  });
});
