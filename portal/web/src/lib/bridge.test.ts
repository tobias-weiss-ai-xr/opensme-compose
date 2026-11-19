import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  getProvider,
  registerProvider,
  resetBridgeForTests,
  useSlot,
  type BridgeProviderMap,
  type CloudPickProvider,
} from "./bridge";

/**
 * Bridge contract (portal-intercom capability).
 *
 * Cross-component integration goes through ONE typed provider registry —
 * never through peer imports. Rules pinned here:
 *  - no provider registered  => getProvider returns null (hosts render no
 *    affordance, fail-soft)
 *  - registerProvider + getProvider roundtrip for both slots
 *  - registrations are replaceable (latest wins) and unregisterable
 *  - module import is SSR-safe (no window/document at top level)
 */

describe("bridge registry", () => {
  beforeEach(() => {
    resetBridgeForTests();
  });

  it("returns null when no provider is registered (fail-soft)", () => {
    expect(getProvider("share.open")).toBeNull();
    expect(getProvider("cloud.pick")).toBeNull();
  });

  it("returns the registered share.open provider", () => {
    const open = vi.fn();
    registerProvider("share.open", { open } satisfies BridgeProviderMap["share.open"]);
    const got = getProvider("share.open");
    expect(got).not.toBeNull();
    got?.open({ text: "Titel", url: "https://x.example/a" });
    expect(open).toHaveBeenCalledWith({
      text: "Titel",
      url: "https://x.example/a",
    });
  });

  it("returns the registered cloud.pick provider", async () => {
    const pick = vi.fn(async () => ({
      path: "/Test/datei.pdf",
      name: "datei.pdf",
      url: "https://cloud.example/s/link",
    }));
    registerProvider("cloud.pick", { pick } satisfies CloudPickProvider);
    const got = getProvider("cloud.pick");
    expect(got).not.toBeNull();
    await got?.pick();
    expect(pick).toHaveBeenCalledOnce();
  });

  it("lets the latest registration win", () => {
    const first = vi.fn();
    const second = vi.fn();
    registerProvider("share.open", { open: first });
    registerProvider("share.open", { open: second });
    getProvider("share.open")?.open({ text: "x" });
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledOnce();
  });

  it("unregisters via the returned cleanup and is then fail-soft", () => {
    const cleanup = registerProvider("cloud.pick", {
      pick: async () => null,
    } satisfies CloudPickProvider);
    expect(getProvider("cloud.pick")).not.toBeNull();
    cleanup();
    expect(getProvider("cloud.pick")).toBeNull();
  });

  it("keeps one slot isolated from the other", () => {
    registerProvider("share.open", { open: vi.fn() });
    expect(getProvider("share.open")).not.toBeNull();
    expect(getProvider("cloud.pick")).toBeNull();
  });

  it("is SSR-safe: renderToStaticMarkup falls back to the null server snapshot", () => {
    // A provider registered before the server render must NOT leak into the
    // server output — the R21-CSP errors proved this class of bug before.
    registerProvider("cloud.pick", { pick: async () => null });
    function Consumer() {
      const picker = useSlot("cloud.pick");
      return createElement("button", null, picker ? "has-provider" : "none");
    }
    const html = renderToStaticMarkup(createElement(Consumer));
    expect(html).toContain("none");
    expect(html).not.toContain("has-provider");
  });
});
