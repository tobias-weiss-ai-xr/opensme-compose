import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import IntercomPanel from "./IntercomPanel";
import { fetchIntercom, postIntercom, postIntercomUpload } from "@/lib/api";
import type { IntercomListResponse } from "@/lib/types";

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

function listResponse(
  messages: IntercomListResponse["messages"] = [],
): IntercomListResponse {
  return { messages };
}

describe("IntercomPanel", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedPost.mockReset();
    mockedUpload.mockReset();
  });

  it("renders the empty state once the initial fetch resolves with []", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    expect(
      await screen.findByText("No notes yet — start the conversation."),
    ).toBeInTheDocument();
    expect(mockedFetch).toHaveBeenCalledTimes(1);
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
      listResponse([
        { text: "Kickoff um 9", attachment: null },
        {
          text: "Deck ist online",
          attachment: {
            url: "https://cloud.example/s/deck.pdf",
            name: "deck.pdf",
            size: 512,
            content_type: "application/pdf",
          },
        },
      ]),
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

  it("keeps the last known list when a refresh after send fails", async () => {
    mockedFetch.mockResolvedValueOnce(
      listResponse([{ text: "Bleibt stehen", attachment: null }]),
    );
    render(<IntercomPanel />);
    expect(await screen.findByText("Bleibt stehen")).toBeInTheDocument();

    mockedPost.mockResolvedValue({ text: "Zweite Notiz", attachment: null });
    mockedFetch.mockRejectedValueOnce(new Error("HTTP 503"));
    fireEvent.change(screen.getByLabelText("Nachricht"), {
      target: { value: "Zweite Notiz" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(2));
    await act(async () => {}); // let the failed refresh settle
    expect(screen.getByText("Bleibt stehen")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
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

  it("sends trimmed text via postIntercom, clears the form and refreshes the list", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    mockedPost.mockResolvedValue({ text: "Hallo Team", attachment: null });
    mockedFetch.mockResolvedValueOnce(
      listResponse([{ text: "Hallo Team", attachment: null }]),
    );
    fireEvent.change(screen.getByLabelText("Nachricht"), {
      target: { value: "  Hallo Team  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));

    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith({ text: "Hallo Team" }),
    );
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Hallo Team")).toBeInTheDocument();
    expect(screen.getByLabelText("Nachricht")).toHaveValue("");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("adds attachment_url when the cloud URL field is filled", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    mockedPost.mockResolvedValue({ text: "Schau mal", attachment: null });
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
    await act(async () => {}); // settle the refresh
  });

  it("uploads a picked file via postIntercomUpload instead of postIntercom", async () => {
    mockedFetch.mockResolvedValue(listResponse());
    render(<IntercomPanel />);
    await screen.findByText(/No notes yet/);

    const file = new File(["%PDF-bytes"], "bericht.pdf", {
      type: "application/pdf",
    });
    mockedUpload.mockResolvedValue({ text: "Mit Datei", attachment: null });
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
    await act(async () => {}); // settle the refresh
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
});
