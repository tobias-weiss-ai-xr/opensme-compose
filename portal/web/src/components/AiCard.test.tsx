import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import AiCard from "./AiCard";
import { chatWithAi } from "@/lib/api";
import type { AiChatResponse } from "@/lib/types";

vi.mock("@/lib/api", () => ({ chatWithAi: vi.fn() }));

const mocked = vi.mocked(chatWithAi);

describe("AiCard", () => {
  beforeEach(() => {
    mocked.mockReset();
  });

  it("renders the assistant card without an answer initially", () => {
    render(<AiCard />);
    expect(screen.getByText("KI-Assistent")).toBeInTheDocument();
    expect(screen.getByLabelText("Frage")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Stellen" })).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("asks the backend with the trimmed question and renders the answer", async () => {
    mocked.mockResolvedValue({ answer: "42 Antworten" });
    render(<AiCard />);
    fireEvent.change(screen.getByLabelText("Frage"), {
      target: { value: "  Was ist openSME?  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Stellen" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("42 Antworten"),
    );
    expect(mocked).toHaveBeenCalledTimes(1);
    expect(mocked).toHaveBeenCalledWith({ question: "Was ist openSME?" });
  });

  it("does not call the backend for empty or whitespace-only input", () => {
    render(<AiCard />);
    fireEvent.click(screen.getByRole("button", { name: "Stellen" }));
    fireEvent.change(screen.getByLabelText("Frage"), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Stellen" }));
    expect(mocked).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("falls back to '(empty answer)' when the backend returns nothing", async () => {
    mocked.mockResolvedValue({ answer: "" });
    render(<AiCard />);
    fireEvent.change(screen.getByLabelText("Frage"), {
      target: { value: "Leere Antwort?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Stellen" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("(empty answer)"),
    );
  });

  it("renders an error message when the backend rejects", async () => {
    mocked.mockRejectedValue(new Error("HTTP 503"));
    render(<AiCard />);
    fireEvent.change(screen.getByLabelText("Frage"), {
      target: { value: "Kaputt?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Stellen" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "AI unavailable: HTTP 503",
      ),
    );
  });

  it("shows the busy state and disables the button until resolved", async () => {
    let resolve!: (value: AiChatResponse) => void;
    mocked.mockImplementation(
      () =>
        new Promise<AiChatResponse>((r) => {
          resolve = r;
        }),
    );
    render(<AiCard />);
    fireEvent.change(screen.getByLabelText("Frage"), {
      target: { value: "Langsam?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Stellen" }));
    const button = screen.getByRole("button");
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("…");
    expect(screen.getByRole("status")).toHaveTextContent("…");
    resolve({ answer: "fertig" });
    await waitFor(() => expect(button).toBeEnabled());
    expect(screen.getByRole("status")).toHaveTextContent("fertig");
  });
});
