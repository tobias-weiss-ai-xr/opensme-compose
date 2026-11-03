import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import IntercomPanel from "./IntercomPanel";
import { fetchIntercom, postIntercom, postIntercomUpload } from "@/lib/api";
import type { IntercomListResponse, IntercomMessage } from "@/lib/types";

const { MockAuthError } = vi.hoisted(() => ({
  MockAuthError: class MockAuthError extends Error {
    status = 401;
    constructor(m = "Anmeldung erforderlich") {
      super(m);
      this.name = "AuthError";
    }
  },
}));

vi.mock("@/lib/api", () => ({
  fetchIntercom: vi.fn(),
  postIntercom: vi.fn(),
  postIntercomUpload: vi.fn(),
  AuthError: MockAuthError,
}));

const AuthError = MockAuthError;
const mockedFetch = vi.mocked(fetchIntercom);
const mockedPost = vi.mocked(postIntercom);
const mockedUpload = vi.mocked(postIntercomUpload);

function msg(partial: Partial<IntercomMessage> & { text: string }): IntercomMessage {
  return { id: 1, attachment: null, ...partial };
}

function listResponse(
  messages: IntercomListResponse["messages"] = [],
  latest = messages.length,
): IntercomListResponse {
  return { messages, latest };
}

/** jsdom has no real visibility — flip document.hidden under test. */
function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    value: hidden,
    configurable: true,
  });
  fireEvent(document, new Event("visibilitychange"));
}

describe("IntercomPanel", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedPost.mockReset();
    mockedUpload.mockReset();
    vi.useRealTimers();
    setHidden(false);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders the empty state once the initial fetch resolves with []", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    expect(
      await screen.findByText("No notes yet — start the conversation."),
    ).toBeInTheDocument();
    expect(mockedFetch).toHaveBeenCalledTimes(1);
    expect(mockedFetch).toHaveBeenCalledWith();
    expect(screen.getByRole("region", { name: "Notizen" })).toBeInTheDocument();
    expect(screen.getByRole("list")).toHaveAttribute("aria-live", "polite");
  });

  it("renders login CTA when the backend answers 401 (session required)", async () => {
    mockedFetch.mockRejectedValue(new AuthError());
    render(<IntercomPanel />);
    expect(
      await screen.findByText(
        "Team-Notizen sind angemeldeten Nutzer:innen vorbehalten",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Anmelden/ }),
    ).toHaveAttribute("href", "/api/auth/login");
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  it("renders message text plus attachment link with humanSize suffix and content type", async () => {
    mockedFetch.mockResolvedValue(
      listResponse(
        [
          msg({ id: 1, text: "Kickoff um 9" }),
          msg({
            id: 2,
            text: "Deck ist online",
            attachment: {
              url: "https://cloud.example/s/deck.pdf",
              name: "deck.pdf",
              size: 512,
              content_type: "application/pdf",
            },
          }),
        ],
        2,
      ),
    );
    render(<IntercomPanel />);
    expect(await screen.findByText("Kickoff um 9")).toBeInTheDocument();
    expect(screen.getByText("Deck ist online")).toBeInTheDocument();
    const link = screen.getByRole("link");
    expect(link).toHaveTextContent("📎 deck.pdf");
    expect(link).toHaveTextContent("(512 B)");
    expect(link).toHaveTextContent("application/pdf");
    expect(link).toHaveAttribute("href", "https://cloud.example/s/deck.pdf");
    expect(link).toHaveAttribute("target", "_blank");
    expect(screen.queryByText(/No notes yet/)).not.toBeInTheDocument();
  });

  it("renders author and relative time as a dim meta line", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockedFetch.mockResolvedValue(
      listResponse([
        msg({
          id: 1,
          text: "Mit Autor",
          author: "Anna",
          created_at: String(now - 180),
        }),
        msg({ id: 2, text: "Ohne Autor", created_at: String(now - 180) }),
        msg({ id: 3, text: "Legacy ohne Meta" }),
      ]),
    );
    render(<IntercomPanel />);
    expect(await screen.findByText("Mit Autor")).toBeInTheDocument();
    expect(screen.getByText("von Anna · vor 3 Min.")).toBeInTheDocument();
    expect(screen.getByText("vor 3 Min.")).toBeInTheDocument();
    // legacy note (no author, no timestamp) renders no meta line at all
    const metaLines = screen.getAllByText(/Min\.|gerade eben/);
    expect(metaLines).toHaveLength(2);
  });

  it("keeps the last known list when a refresh after send fails", async () => {
    mockedFetch.mockResolvedValueOnce(
      listResponse([msg({ id: 7, text: "Bleibt stehen" })], 7),
    );
    render(<IntercomPanel />);
    expect(await screen.findByText("Bleibt stehen")).toBeInTheDocument();

    mockedPost.mockRejectedValue(new Error("HTTP 503"));
    fireEvent.change(screen.getByLabelText("Nachricht"), {
      target: { value: "Zweite Notiz" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await waitFor(() =>
      expect(
        screen.getByRole("status"),
      ).toHaveTextContent("Not sent: HTTP 503"),
    );
    expect(screen.getByText("Bleibt stehen")).toBeInTheDocument();
  });

  it("blocks empty submissions with a hint and no API call", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    fireEvent.change(screen.getByLabelText("Nachricht"), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Write a short note first.",
    );
    expect(mockedPost).not.toHaveBeenCalled();
    expect(mockedUpload).not.toHaveBeenCalled();
    expect(mockedFetch).toHaveBeenCalledTimes(1); // initial load only
  });

  it("prepends the returned message after sending — no extra fetch roundtrip", async () => {
    mockedFetch.mockResolvedValue(listResponse([msg({ id: 4, text: "Alt" })], 4));
    render(<IntercomPanel />);
    await screen.findByText("Alt");

    mockedPost.mockResolvedValue({
      message: msg({ id: 5, text: "Hallo Team", author: "Bob" }),
    });
    fireEvent.change(screen.getByLabelText("Nachricht"), {
      target: { value: "  Hallo Team  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));

    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith({ text: "Hallo Team" }),
    );
    expect(await screen.findByText("Hallo Team")).toBeInTheDocument();
    expect(screen.getByLabelText("Nachricht")).toHaveValue("");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    // the send used the POST response — no second list fetch
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });

  it("adds attachment_url when the cloud URL field is filled", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    mockedPost.mockResolvedValue({
      message: msg({ id: 1, text: "Schau mal" }),
    });
    fireEvent.change(screen.getByLabelText("Nachricht"), {
      target: { value: "Schau mal" },
    });
    fireEvent.change(screen.getByLabelText("Anhang-URL"), {
      target: { value: "https://cloud.example/s/abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith({
        text: "Schau mal",
        attachment_url: "https://cloud.example/s/abc",
      }),
    );
    expect(mockedUpload).not.toHaveBeenCalled();
  });

  it("uploads a picked file via postIntercomUpload instead of postIntercom", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    const file = new File(["%PDF-bytes"], "bericht.pdf", {
      type: "application/pdf",
    });
    mockedUpload.mockResolvedValue({
      message: msg({ id: 1, text: "Mit Datei" }),
    });
    const fileInput = screen.getByLabelText("Datei anhängen");
    fireEvent.change(fileInput, { target: { files: [file] } });
    expect(screen.getByText(/bericht\.pdf/)).toBeInTheDocument(); // picked-file hint

    fireEvent.change(screen.getByLabelText("Nachricht"), {
      target: { value: "Mit Datei" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await waitFor(() => expect(mockedUpload).toHaveBeenCalledTimes(1));
    expect(mockedUpload).toHaveBeenCalledWith("Mit Datei", file);
    expect(mockedPost).not.toHaveBeenCalled();
  });

  it("surfaces the error message when sending fails and keeps the text", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    mockedPost.mockRejectedValue(new Error("HTTP 500"));
    fireEvent.change(screen.getByLabelText("Nachricht"), {
      target: { value: "Geht nicht" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Not sent: HTTP 500",
    );
    expect(screen.getByLabelText("Nachricht")).toHaveValue("Geht nicht");
  });

  // ── delta polling ────────────────────────────────────────────────

  it("polls the delta endpoint every 7s while the tab is visible", async () => {
    vi.useFakeTimers();
    mockedFetch.mockResolvedValue(
      listResponse([msg({ id: 3, text: "Start" })], 3),
    );
    render(<IntercomPanel />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockedFetch).toHaveBeenCalledTimes(1);
    expect(mockedFetch).toHaveBeenCalledWith();

    mockedFetch.mockResolvedValueOnce(
      listResponse([msg({ id: 4, text: "Neu per Delta" })], 4),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(7000);
    });
    expect(mockedFetch).toHaveBeenLastCalledWith(3);
    expect(screen.getByText("Neu per Delta")).toBeInTheDocument();
    expect(screen.getByText("Start")).toBeInTheDocument();
  });

  it("skips polling while the tab is hidden and catches up on show", async () => {
    vi.useFakeTimers();
    mockedFetch.mockResolvedValue(listResponse([msg({ id: 1, text: "A" })], 1));
    render(<IntercomPanel />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    act(() => setHidden(true));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000); // 4+ intervals — all skipped
    });
    expect(mockedFetch).toHaveBeenCalledTimes(1);

    mockedFetch.mockResolvedValueOnce(listResponse([], 1));
    act(() => setHidden(false)); // visibilitychange → immediate delta
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockedFetch).toHaveBeenLastCalledWith(1);
  });

  it("merges delta notes at the top without duplicates", async () => {
    vi.useFakeTimers();
    mockedFetch.mockResolvedValue(
      listResponse([msg({ id: 2, text: "Basis" })], 2),
    );
    render(<IntercomPanel />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    // delta returns id 3 twice (send-response race) — must render once
    mockedFetch.mockResolvedValueOnce(
      listResponse([msg({ id: 3, text: "Einmal" })], 3),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(7000);
    });
    expect(screen.getAllByText("Einmal")).toHaveLength(1);
    expect(screen.getByText("Basis")).toBeInTheDocument();
  });

  it("recovers with a full refetch when the store restarted behind us", async () => {
    vi.useFakeTimers();
    mockedFetch.mockResolvedValue(
      listResponse([msg({ id: 50, text: "Alte Welt" })], 50),
    );
    render(<IntercomPanel />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    // store reset: latest (2) < cursor (50) → delta comes back empty with a
    // lower latest → full list, not another delta
    mockedFetch.mockResolvedValueOnce(listResponse([], 2));
    mockedFetch.mockResolvedValueOnce(
      listResponse([msg({ id: 1, text: "Neue Welt" })], 2),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(7000);
    });
    expect(mockedFetch).toHaveBeenCalledWith();
    expect(screen.getByText("Neue Welt")).toBeInTheDocument();
    expect(screen.queryByText("Alte Welt")).not.toBeInTheDocument();
  });

  it("stops polling after a 401 mid-session", async () => {
    vi.useFakeTimers();
    mockedFetch.mockResolvedValue(listResponse([msg({ id: 1, text: "A" })], 1));
    render(<IntercomPanel />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    mockedFetch.mockRejectedValueOnce(new AuthError());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(7000);
    });
    expect(
      screen.getByText(
        "Team-Notizen sind angemeldeten Nutzer:innen vorbehalten",
      ),
    ).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(21_000);
    });
    expect(mockedFetch).toHaveBeenCalledTimes(2); // initial + the 401 poll, then dead
  });
});
