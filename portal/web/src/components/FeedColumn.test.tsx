import { render, screen, act } from "@testing-library/react";
import FeedColumn from "./FeedColumn";
import { fetchFeeds } from "@/lib/api";
import type { FeedResponse } from "@/lib/types";

vi.mock("@/lib/api", () => ({ fetchFeeds: vi.fn() }));

const mocked = vi.mocked(fetchFeeds);

const startupColumn = {
  id: "startup",
  title: "Startup",
  entries: [
    {
      title: "Neuer Bericht",
      url: "https://news.example/bericht",
      date: "2025-01-01T00:00:00Z",
      source: "t3n.de",
    },
    {
      title: "Ohne Datum",
      url: "https://news.example/ohne-datum",
      date: null,
      source: "lto.de",
    },
  ],
};

function response(columns = [startupColumn]): FeedResponse {
  return { columns };
}

/** Section elements carry an implicit region role once labelled. */
function columnCard() {
  return document.querySelector<HTMLElement>(
    "div.feed-card[data-feed-column]",
  ) as HTMLElement;
}

describe("FeedColumn", () => {
  beforeEach(() => {
    mocked.mockReset();
  });

  it("shows the loading state until the feed resolves", () => {
    mocked.mockImplementation(() => new Promise<FeedResponse>(() => {}));
    render(<FeedColumn placeholder={{ id: "startup", title: "Startup" }} />);
    expect(mocked).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("heading", { level: 2, name: "Startup" })).toBeInTheDocument();
    expect(screen.getByText("Lade Meldungen…")).toBeInTheDocument();
    expect(columnCard()).toHaveAttribute("data-feed-column", "startup");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders entries as safe links with href and formatted meta label", async () => {
    mocked.mockResolvedValue(response());
    render(<FeedColumn placeholder={{ id: "startup", title: "Startup" }} />);
    const link = await screen.findByRole("link", { name: "Neuer Bericht" });
    expect(link).toHaveAttribute("href", "https://news.example/bericht");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    // "dd.MM.yyyy · source" / bare source — the legacy app.js label shape.
    expect(screen.getByText("01.01.2025 · t3n.de")).toBeInTheDocument();
    expect(screen.getByText("lto.de")).toBeInTheDocument();
    expect(screen.getByRole("list")).toHaveAttribute("aria-live", "polite");
    expect(screen.queryByText("Lade Meldungen…")).not.toBeInTheDocument();
  });

  it("renders the error state when fetchFeeds rejects", async () => {
    mocked.mockRejectedValue(new Error("HTTP 503"));
    render(<FeedColumn placeholder={{ id: "markt", title: "Markt" }} />);
    const error = await screen.findByText("Feed derzeit nicht verfügbar");
    expect(error).toBeInTheDocument();
    expect(error).toHaveClass("error");
    expect(screen.queryByText("Lade Meldungen…")).not.toBeInTheDocument();
  });

  it("falls back to the placeholder title with an empty list for unknown ids", async () => {
    mocked.mockResolvedValue(response([]));
    render(<FeedColumn placeholder={{ id: "legal", title: "Legal" }} />);
    await act(async () => {}); // let the effect settle
    expect(screen.getByRole("heading", { level: 2, name: "Legal" })).toBeInTheDocument();
    expect(columnCard()).toHaveAttribute("data-feed-column", "legal");
    expect(screen.getByText("Lade Meldungen…")).toBeInTheDocument();
    expect(screen.queryByText("Feed derzeit nicht verfügbar")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("re-fetches after 10 minutes and clears the interval on unmount", async () => {
    vi.useFakeTimers();
    const clearSpy = vi.spyOn(globalThis, "clearInterval");
    mocked.mockResolvedValue(response());
    const { unmount } = render(
      <FeedColumn placeholder={{ id: "startup", title: "Startup" }} />,
    );
    await act(async () => {}); // flush the initial load
    expect(mocked).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(10 * 60 * 1000);
    });
    expect(mocked).toHaveBeenCalledTimes(2);

    unmount();
    expect(clearSpy).toHaveBeenCalled();
    clearSpy.mockRestore();
    vi.useRealTimers();
  });
});
