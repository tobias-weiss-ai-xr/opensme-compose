import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import CloudPickerProvider from "./CloudPickerProvider";
import { getProvider, resetBridgeForTests } from "@/lib/bridge";

const CLOUD_FILE = {
  name: "kickoff.pdf",
  size: 4096,
  mime: "application/pdf",
  path: "docs/kickoff.pdf",
};

/**
 * CloudPickerProvider owns the shared cloud-file selection slot
 * (cloud.pick): it mounts the real picker modal and registers the
 * provider with the bridge — consumers call provider.pick() and get the
 * selected entry (or null when closed). One provider module, every
 * attachment surface consumes the same slot.
 */
function stubList() {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify([CLOUD_FILE]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

describe("CloudPickerProvider", () => {
  beforeEach(() => {
    resetBridgeForTests();
    vi.unstubAllGlobals();
  });

  it("registers the cloud.pick provider on mount and unregisters on unmount", async () => {
    expect(getProvider("cloud.pick")).toBeNull();
    const { unmount } = render(<CloudPickerProvider />);
    expect(getProvider("cloud.pick")).not.toBeNull();
    unmount();
    expect(getProvider("cloud.pick")).toBeNull();
  });

  it("resolves pick() with the selected cloud entry", async () => {
    stubList();
    render(<CloudPickerProvider />);
    const provider = getProvider("cloud.pick");
    expect(provider).not.toBeNull();

    let result!: Promise<import("@/lib/bridge").CloudPickResult | null>;
    act(() => {
      result = provider!.pick();
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 80)); // let the listing load
    });
    const row = screen.getByRole("button", { name: /kickoff\.pdf/ });
    fireEvent.click(row);
    await expect(result).resolves.toEqual({
      path: "docs/kickoff.pdf",
      name: "kickoff.pdf",
      size: 4096,
    });
  });

  it("resolves pick() with null when the modal is closed (cancelled)", async () => {
    stubList();
    render(<CloudPickerProvider />);
    const provider = getProvider("cloud.pick");
    let result!: Promise<import("@/lib/bridge").CloudPickResult | null>;
    act(() => {
      result = provider!.pick();
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 80));
    });
    fireEvent.click(screen.getByRole("button", { name: "Schließen" }));
    await expect(result).resolves.toBeNull();
  });
});
