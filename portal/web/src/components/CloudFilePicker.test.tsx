import { render, screen, fireEvent } from "@testing-library/react";
import CloudFilePicker from "./CloudFilePicker";
import type { CloudFileEntry } from "./CloudFilePicker";

const FILES: CloudFileEntry[] = [
  {
    name: "bericht.pdf",
    size: 2048,
    mime: "application/pdf",
    path: "docs/bericht.pdf",
  },
  {
    name: "logo.png",
    size: null,
    mime: "image/png",
    path: "assets/logo.png",
  },
];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPicker(overrides: {
  open?: boolean;
  onClose?: () => void;
  onSelect?: (file: CloudFileEntry) => void;
} = {}) {
  const onClose = overrides.onClose ?? vi.fn();
  const onSelect = overrides.onSelect ?? vi.fn();
  const view = render(
    <CloudFilePicker
      open={overrides.open ?? true}
      onClose={onClose}
      onSelect={onSelect}
    />,
  );
  return { view, onClose, onSelect };
}

describe("CloudFilePicker", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders nothing while closed and never fetches", () => {
    renderPicker({ open: false });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("opens a modal and lists the files from /api/cloud/list", async () => {
    fetchMock.mockResolvedValue(jsonResponse(FILES));
    renderPicker();
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("bericht.pdf")).toBeInTheDocument();
    expect(screen.getByText("logo.png")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/cloud/list");
  });

  it("shows file sizes in human-readable form", async () => {
    fetchMock.mockResolvedValue(jsonResponse(FILES));
    renderPicker();
    await screen.findByText("bericht.pdf");
    expect(screen.getByText(/2\.0 KB/)).toBeInTheDocument();
    // null size → no size suffix at all
    expect(screen.getByText(/logo\.png/)).not.toHaveTextContent("(");
  });

  it("hands the exact file payload to onSelect and closes", async () => {
    fetchMock.mockResolvedValue(jsonResponse(FILES));
    const { onSelect, onClose } = renderPicker();
    fireEvent.click(await screen.findByRole("button", { name: /bericht\.pdf/ }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(FILES[0]);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("shows the German empty state for an empty directory", async () => {
    fetchMock.mockResolvedValue(jsonResponse([]));
    renderPicker();
    expect(await screen.findByText("Keine Dateien in der Cloud")).toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  it("shows a German error and recovers via retry", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({ error: "Cloud nicht erreichbar" }, 502),
      )
      .mockResolvedValueOnce(jsonResponse(FILES));
    renderPicker();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Cloud-Liste fehlgeschlagen (Cloud nicht erreichbar)",
    );
    fireEvent.click(screen.getByRole("button", { name: "Erneut versuchen" }));
    expect(await screen.findByText("bericht.pdf")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("closes on Escape", async () => {
    fetchMock.mockResolvedValue(jsonResponse(FILES));
    const { onClose } = renderPicker();
    await screen.findByRole("dialog");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes via Abbrechen and via the ✕ button", async () => {
    fetchMock.mockResolvedValue(jsonResponse(FILES));
    const { onClose, view } = renderPicker();
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "Abbrechen" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Schließen" }));
    expect(onClose).toHaveBeenCalledTimes(2);
    // sanity: the modal is still mounted until the parent unmounts it
    expect(view.container.querySelector(".cfp-overlay")).not.toBeNull();
  });

  it("closes on backdrop clicks but not on clicks inside the dialog", async () => {
    fetchMock.mockResolvedValue(jsonResponse(FILES));
    const { onClose, view } = renderPicker();
    await screen.findByRole("dialog");
    const overlay = view.container.querySelector(".cfp-overlay") as HTMLElement;
    fireEvent.mouseDown(overlay);
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.mouseDown(screen.getByRole("dialog"));
    expect(onClose).toHaveBeenCalledTimes(1); // unchanged — inner click swallowed
  });
});
