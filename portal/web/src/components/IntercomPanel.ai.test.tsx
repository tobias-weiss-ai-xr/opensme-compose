import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import IntercomPanel from "./IntercomPanel";
import { fetchIntercom } from "@/lib/api";
import { aiEnabledProbe, draftReply, summarizeThread } from "@/lib/ai";
import type { IntercomListResponse, IntercomMessage } from "@/lib/types";

/**
 * AI helper row (T6): the existing IntercomPanel suite stays untouched —
 * this file covers only the opt-in AI surface ('Zusammenfassen' /
 * 'Antwort vorschlagen', hidden unless the probe says enabled).
 */

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
  postCloudShare: vi.fn(),
  postBroadcast: vi.fn(),
  AuthError: MockAuthError,
}));

vi.mock("@/lib/ai", () => ({
  aiEnabledProbe: vi.fn(),
  summarizeThread: vi.fn(),
  draftReply: vi.fn(),
}));

const AuthError = MockAuthError;
const mockedFetch = vi.mocked(fetchIntercom);
const mockedProbe = vi.mocked(aiEnabledProbe);
const mockedSummarize = vi.mocked(summarizeThread);
const mockedDraft = vi.mocked(draftReply);

function msg(partial: Partial<IntercomMessage> & { text: string }): IntercomMessage {
  return { id: 1, attachment: null, ...partial };
}

function listResponse(
  messages: IntercomListResponse["messages"] = [],
  latest = messages.length,
): IntercomListResponse {
  return { messages, latest };
}

describe("IntercomPanel AI helper row", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedProbe.mockReset();
    mockedSummarize.mockReset();
    mockedDraft.mockReset();
    mockedProbe.mockResolvedValue(false);
    mockedFetch.mockResolvedValue(
      listResponse([msg({ id: 5, text: "Neueste Notiz" }), msg({ id: 4, text: "Älter" })], 5),
    );
  });

  it("hides the AI row when the probe answers disabled (no error spam)", async () => {
    mockedProbe.mockResolvedValue(false);
    render(<IntercomPanel />);
    expect(await screen.findByText("Neueste Notiz")).toBeInTheDocument();
    await waitFor(() => expect(mockedProbe).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: /Zusammenfassen/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Antwort vorschlagen/ })).not.toBeInTheDocument();
    // silence is the contract: no AI status line, even after the probe settles
    expect(screen.queryByText(/KI-Fehler/)).not.toBeInTheDocument();
  });

  it("keeps the row hidden when the probe throws (route down, offline)", async () => {
    mockedProbe.mockRejectedValue(new Error("network down"));
    render(<IntercomPanel />);
    expect(await screen.findByText("Neueste Notiz")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Zusammenfassen/ })).not.toBeInTheDocument();
  });

  it("shows both buttons once the probe reports the AI enabled", async () => {
    mockedProbe.mockResolvedValue(true);
    render(<IntercomPanel />);
    expect(
      await screen.findByRole("button", { name: /Zusammenfassen/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Antwort vorschlagen/ })).toBeInTheDocument();
  });

  it("summarizes the thread anchored on the newest note", async () => {
    mockedProbe.mockResolvedValue(true);
    mockedSummarize.mockResolvedValue({ summary: "Bob deployt, Anna reviewt." });
    render(<IntercomPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Zusammenfassen/ }));
    await waitFor(() => expect(mockedSummarize).toHaveBeenCalledWith(5));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Bob deployt, Anna reviewt.",
    );
    expect(mockedDraft).not.toHaveBeenCalled();
  });

  it("suggests a draft reply via the draft route", async () => {
    mockedProbe.mockResolvedValue(true);
    mockedDraft.mockResolvedValue({ draft: "Mache ich heute noch." });
    render(<IntercomPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Antwort vorschlagen/ }));
    await waitFor(() => expect(mockedDraft).toHaveBeenCalledWith(5));
    expect(await screen.findByRole("status")).toHaveTextContent("Mache ich heute noch.");
  });

  it("surfaces AI failures as a German inline status and keeps the notes", async () => {
    mockedProbe.mockResolvedValue(true);
    mockedSummarize.mockRejectedValue(new Error("KI nicht erreichbar"));
    render(<IntercomPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Zusammenfassen/ }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "KI-Fehler: KI nicht erreichbar",
    );
    expect(screen.getByText("Neueste Notiz")).toBeInTheDocument();
  });

  it("falls back to the login CTA on 401 during an AI call", async () => {
    mockedProbe.mockResolvedValue(true);
    mockedSummarize.mockRejectedValue(new AuthError());
    render(<IntercomPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Zusammenfassen/ }));
    expect(
      await screen.findByText(
        "Team-Notizen sind angemeldeten Nutzer:innen vorbehalten",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Zusammenfassen/ })).not.toBeInTheDocument();
  });

  it("disables both buttons while a request is in flight", async () => {
    mockedProbe.mockResolvedValue(true);
    let resolve!: (v: { summary: string }) => void;
    mockedSummarize.mockReturnValue(
      new Promise<{ summary: string }>((r) => {
        resolve = r;
      }),
    );
    render(<IntercomPanel />);
    const sum = await screen.findByRole("button", { name: /Zusammenfassen/ });
    fireEvent.click(sum);
    await waitFor(() => expect(sum).toBeDisabled());
    expect(screen.getByRole("button", { name: /Antwort vorschlagen/ })).toBeDisabled();
    resolve({ summary: "done" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Zusammenfassen/ })).toBeEnabled(),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("done");
  });

  it("asks for a note first when the thread is empty", async () => {
    mockedProbe.mockResolvedValue(true);
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    const sum = await screen.findByRole("button", { name: /Zusammenfassen/ });
    fireEvent.click(sum);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Keine Notizen vorhanden — erst etwas notieren.",
    );
    expect(mockedSummarize).not.toHaveBeenCalled();
  });
});
