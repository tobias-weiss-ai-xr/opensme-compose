import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import IntercomPanel from "./IntercomPanel";
import {
  fetchIntercom,
  postBroadcast,
  postCloudShare,
  postIntercom,
  postIntercomUpload,
} from "@/lib/api";
import type { IntercomListResponse, IntercomMessage } from "@/lib/types";
import { registerProvider, resetBridgeForTests } from "@/lib/bridge";

const { MockAuthError, mockedCloudUpload } = vi.hoisted(() => ({
  MockAuthError: class MockAuthError extends Error {
    status = 401;
    constructor(m = "Anmeldung erforderlich") {
      super(m);
      this.name = "AuthError";
    }
  },
  mockedCloudUpload: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  fetchIntercom: vi.fn(),
  postIntercom: vi.fn(),
  postIntercomUpload: vi.fn(),
  postCloudShare: vi.fn(),
  uploadCloudFile: mockedCloudUpload,
  postBroadcast: vi.fn(),
  AuthError: MockAuthError,
}));

/**
 * T6: the panel probes the AI gate on mount (@/lib/ai.aiEnabledProbe).
 * Without this mock the real probe would hit the tests' global fetch
 * stubs (e.g. the cloud-picker list Response) and race their bodies.
 * Disabled by default → the AI row stays out of every legacy assertion.
 */
vi.mock("@/lib/ai", () => ({
  aiEnabledProbe: vi.fn(async () => false),
  summarizeThread: vi.fn(),
  draftReply: vi.fn(),
}));

const AuthError = MockAuthError;
const mockedFetch = vi.mocked(fetchIntercom);
const mockedPost = vi.mocked(postIntercom);
const mockedIntercomUpload = vi.mocked(postIntercomUpload);
const mockedShare = vi.mocked(postCloudShare);
const mockedBroadcast = vi.mocked(postBroadcast);

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
    resetBridgeForTests();
    mockedFetch.mockReset();
    mockedPost.mockReset();
    mockedIntercomUpload.mockReset();
    mockedShare.mockReset();
    mockedBroadcast.mockReset();
    mockedCloudUpload.mockReset();
    vi.useRealTimers();
    setHidden(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
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


  it("offers 'Als Aufgabe anlegen' per note through the task.create slot", async () => {
    const create = vi.fn();
    registerProvider("task.create", { create });
    mockedFetch.mockResolvedValue(
      listResponse([
        msg({
          id: 5,
          text: "Eine lange Notiz über die Rechnungsprüfung und die Belege",
          attachment: {
            url: "https://cloud.example/s/note-link",
            name: "beleg.pdf",
            size: 1024,
            content_type: "application/pdf",
          },
        }),
      ]),
    );
    render(<IntercomPanel />);
    const btns = await screen.findAllByRole("button", {
      name: /Als Aufgabe anlegen/,
    });
    expect(btns).toHaveLength(1);
    act(() => btns[0].click());
    expect(create).toHaveBeenCalledWith({
      title: "Eine lange Notiz über die Rechnungsprüfung und die Belege",
      url: "https://cloud.example/s/note-link",
    });
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
    expect(mockedIntercomUpload).not.toHaveBeenCalled();
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
    expect(mockedIntercomUpload).not.toHaveBeenCalled();
  });

  it("uploads a picked file via postIntercomUpload instead of postIntercom", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    const file = new File(["%PDF-bytes"], "bericht.pdf", {
      type: "application/pdf",
    });
    mockedIntercomUpload.mockResolvedValue({
      message: msg({ id: 1, text: "Mit Datei" }),
    });
    const fileInput = screen.getByLabelText("Datei anhängen");
    fireEvent.change(fileInput, { target: { files: [file] } });
    expect(screen.getByText(/bericht\.pdf/)).toBeInTheDocument(); // picked-file hint

    fireEvent.change(screen.getByLabelText("Nachricht"), {
      target: { value: "Mit Datei" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await waitFor(() => expect(mockedIntercomUpload).toHaveBeenCalledTimes(1));
    expect(mockedIntercomUpload).toHaveBeenCalledWith("Mit Datei", file);
    expect(mockedPost).not.toHaveBeenCalled();
  });

  it("attaches a cloud pick as a share link via postCloudShare", async () => {
    registerProvider("cloud.pick", {
      pick: async () => ({
        path: "docs/kickoff.pdf",
        name: "kickoff.pdf",
        size: 2048,
      }),
    });
    mockedFetch.mockResolvedValue(listResponse());
    mockedShare.mockResolvedValue({ url: "https://cloud.example/s/xyz" });
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    fireEvent.click(
      screen.getByRole("button", { name: /Aus der Cloud anhängen/ }),
    );
    await waitFor(() =>
      expect(mockedShare).toHaveBeenCalledWith("docs/kickoff.pdf"),
    );
    expect(screen.getByLabelText("Anhang-URL")).toHaveValue(
      "https://cloud.example/s/xyz",
    );
    expect(screen.getByText(/☁ kickoff\.pdf/)).toBeInTheDocument();
  });

  it("surfaces a German error when the cloud share creation fails", async () => {
    registerProvider("cloud.pick", {
      pick: async () => ({
        path: "docs/kickoff.pdf",
        name: "kickoff.pdf",
        size: 2048,
      }),
    });
    mockedFetch.mockResolvedValue(listResponse());
    mockedShare.mockRejectedValue(new Error("HTTP 500"));
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    fireEvent.click(
      screen.getByRole("button", { name: /Aus der Cloud anhängen/ }),
    );
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Cloud-Freigabe fehlgeschlagen: HTTP 500",
      ),
    );
    expect(screen.getByLabelText("Anhang-URL")).toHaveValue("");
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

  // ── operator broadcast (INTERCOM_BROADCAST=1) ────────────────────

  it("hides the broadcast box unless broadcastEnabled is set", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);
    expect(screen.queryByLabelText("Broadcast an alle")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Broadcast an alle" }),
    ).not.toBeInTheDocument();
  });

  it("broadcasts: stores the note, prepends it, reports both fanout channels", async () => {
    mockedFetch.mockResolvedValue(listResponse([msg({ id: 1, text: "Alt" })], 1));
    render(<IntercomPanel broadcastEnabled />);
    await screen.findByText("Alt");

    mockedBroadcast.mockResolvedValue({
      message: msg({ id: 9, text: "Alle an Bord", author: "op" }),
      fanout: {
        element: { ok: true, detail: "event $abc → !room:hs" },
        mail: { ok: true, detail: "broadcast delivered to 14 recipient(s)" },
      },
    });
    fireEvent.change(screen.getByLabelText("Broadcast an alle"), {
      target: { value: "  Alle an Bord  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Broadcast an alle" }));

    await waitFor(() =>
      expect(mockedBroadcast).toHaveBeenCalledWith("Alle an Bord"),
    );
    expect(await screen.findByText("Alle an Bord")).toBeInTheDocument(); // prepended note
    expect(
      await screen.findByText("Broadcast gesendet — Element ✓ · Mail ✓"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Broadcast an alle")).toHaveValue("");
    // notes list untouched apart from the prepended broadcast
    expect(screen.getByText("Alt")).toBeInTheDocument();
  });

  it("names a failed fanout channel with its detail, note still stored", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel broadcastEnabled />);
    await screen.findByText(/No notes yet/);

    mockedBroadcast.mockResolvedValue({
      message: msg({ id: 2, text: "Teil-Ok" }),
      fanout: {
        element: { ok: false, detail: "send failed (HTTP 403)" },
        mail: { ok: true, detail: "delivered to 14 recipient(s)" },
      },
    });
    fireEvent.change(screen.getByLabelText("Broadcast an alle"), {
      target: { value: "Teil-Ok" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Broadcast an alle" }));

    expect(
      await screen.findByText(
        "Broadcast gesendet, aber: Element ✗ (send failed (HTTP 403)) · Mail ✓",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Teil-Ok")).toBeInTheDocument();
  });

  it("blocks empty broadcasts with a hint and no API call", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel broadcastEnabled />);
    await screen.findByText(/No notes yet/);

    fireEvent.change(screen.getByLabelText("Broadcast an alle"), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Broadcast an alle" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Erst einen kurzen Broadcast-Text schreiben.",
    );
    expect(mockedBroadcast).not.toHaveBeenCalled();
  });

  it("surfaces broadcast send errors — including the login CTA on 401", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel broadcastEnabled />);
    await screen.findByText(/No notes yet/);

    mockedBroadcast.mockRejectedValue(new Error("HTTP 404"));
    fireEvent.change(screen.getByLabelText("Broadcast an alle"), {
      target: { value: "Geht nicht raus" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Broadcast an alle" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Nicht gesendet: HTTP 404",
    );
    expect(screen.getByLabelText("Broadcast an alle")).toHaveValue("Geht nicht raus");

    mockedBroadcast.mockRejectedValue(new AuthError());
    fireEvent.click(screen.getByRole("button", { name: "Broadcast an alle" }));
    expect(
      await screen.findByText(
        "Team-Notizen sind angemeldeten Nutzer:innen vorbehalten",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Broadcast an alle")).not.toBeInTheDocument();
  });

  it("applies a share prefill to the composer (title+link in text, attachment in the URL field)", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(
      <IntercomPanel
        sharePrefill={{
          text: "t3n — KI-Briefing",
          url: "https://feeds.example/a1",
          attachmentUrl: "https://cloud.example/s/abc",
        }}
      />,
    );
    await screen.findByText("No notes yet — start the conversation.");
    const ta = screen.getByLabelText("Nachricht") as HTMLTextAreaElement;
    expect(ta.value).toContain("t3n — KI-Briefing");
    expect(ta.value).toContain("https://feeds.example/a1");
    expect(
      (screen.getByLabelText("Anhang-URL") as HTMLInputElement).value,
    ).toBe("https://cloud.example/s/abc");
    expect(screen.getByRole("status")).toHaveTextContent("übernommen");
  });

  it("applies a second distinct prefill — the latest share wins", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    const { rerender } = render(
      <IntercomPanel sharePrefill={{ text: "Erste Quelle", url: "https://a.example/1" }} />,
    );
    await screen.findByText("No notes yet — start the conversation.");
    rerender(
      <IntercomPanel sharePrefill={{ text: "Zweite Quelle", url: "https://a.example/2" }} />,
    );
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Nachricht") as HTMLTextAreaElement).value,
      ).toContain("Zweite Quelle"),
    );
  });

  it("hides the cloud-pick trigger while no cloud.pick provider is registered (fail-soft)", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);
    expect(
      screen.queryByRole("button", { name: /Aus der Cloud anhängen/ }),
    ).not.toBeInTheDocument();
  });

  it("keeps the prefilled share content when the post fails — retry without retyping", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    mockedPost.mockRejectedValue(new Error("HTTP 503"));
    render(
      <IntercomPanel
        sharePrefill={{ text: "t3n — KI-Briefing", url: "https://feeds.example/a1" }}
      />,
    );
    await screen.findByText("No notes yet — start the conversation.");
    const ta = screen.getByLabelText("Nachricht") as HTMLTextAreaElement;
    expect(ta.value).toContain("t3n — KI-Briefing");

    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Not sent: HTTP 503"),
    );
    // the prefilled note survives the failed post so the user can retry
    expect(
      (screen.getByLabelText("Nachricht") as HTMLTextAreaElement).value,
    ).toContain("t3n — KI-Briefing");
  });

  it("stores a picked local file in the cloud on demand ('In Cloud ablegen') and switches the note to the cloud link", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    mockedCloudUpload.mockResolvedValue({ path: "bericht.pdf" });
    mockedShare.mockResolvedValue({ url: "https://cloud.example/s/durable" });
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    const file = new File(["inhalt"], "bericht.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(screen.getByLabelText("Datei anhängen"), {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByRole("button", { name: /In Cloud ablegen/ }));

    await waitFor(() =>
      expect(mockedCloudUpload).toHaveBeenCalledWith(
        "bericht.pdf",
        expect.anything(),
        "application/pdf",
      ),
    );
    expect(mockedShare).toHaveBeenCalledWith("bericht.pdf");
    // the note now carries the durable cloud share instead of an upload
    expect(screen.getByLabelText("Anhang-URL")).toHaveValue(
      "https://cloud.example/s/durable",
    );
    // the local file slot is consumed — the ☁ cloud reference stays
    expect(screen.queryByText(/^bericht\.pdf \(/)).not.toBeInTheDocument();
    expect(screen.getByText(/☁ bericht\.pdf/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("In der Cloud abgelegt");
    // the local file slot is consumed — posting uses the cloud link
    expect(mockedIntercomUpload).not.toHaveBeenCalled();
  });
});
