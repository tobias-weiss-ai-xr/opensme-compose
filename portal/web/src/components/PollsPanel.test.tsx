import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import PollsPanel from "./PollsPanel";
import { markVoted, votedSet } from "@/lib/format";
import type { Poll, PollListResponse } from "@/lib/types";

// humanSize stays real; vote bookkeeping is mocked so tests can observe it.
vi.mock("@/lib/format", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/format")>();
  return { ...actual, markVoted: vi.fn(), votedSet: vi.fn(() => ({})) };
});

const mockedMarkVoted = vi.mocked(markVoted);
const mockedVotedSet = vi.mocked(votedSet);

const fetchMock = vi.fn();

function res(ok: boolean, body: unknown, status = 200): Response {
  return { ok, status, json: async () => body } as unknown as Response;
}

function pollList(polls: Poll[]): PollListResponse {
  return { polls };
}

const poll: Poll = {
  id: 7,
  question: "Welches Logo?",
  options: ["Blau", "Grün"],
  votes: [3, 1],
  closed: false,
  attachment: null,
};

describe("PollsPanel", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    mockedMarkVoted.mockReset();
    mockedVotedSet.mockReset().mockReturnValue({});
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the loading placeholder while the initial fetch is pending", () => {
    fetchMock.mockImplementation(() => new Promise<Response>(() => {}));
    render(<PollsPanel />);
    expect(screen.getByText("Umfragen werden geladen…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Anlegen" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/polls", { cache: "no-store" });
  });

  it("renders the empty placeholder for an empty list", async () => {
    fetchMock.mockResolvedValue(res(true, pollList([])));
    render(<PollsPanel />);
    expect(await screen.findByText(/Noch keine Umfragen/)).toBeInTheDocument();
    expect(screen.queryByText(/Umfragen werden geladen/)).not.toBeInTheDocument();
  });

  it("renders polls with question, options, counts and vote buttons", async () => {
    fetchMock.mockResolvedValue(res(true, pollList([poll])));
    render(<PollsPanel />);
    expect(await screen.findByText("Welches Logo?")).toBeInTheDocument();
    expect(screen.getByText("Blau")).toBeInTheDocument();
    expect(screen.getByText("Grün")).toBeInTheDocument();
    expect(screen.getByText("3 · 75%")).toBeInTheDocument();
    expect(screen.getByText("1 · 25%")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Für „Blau“ stimmen" }),
    ).toBeInTheDocument();
  });

  it("hides vote buttons for polls already marked voted", async () => {
    mockedVotedSet.mockReturnValue({ 7: true });
    fetchMock.mockResolvedValue(res(true, pollList([poll])));
    render(<PollsPanel />);
    await screen.findByText("Welches Logo?");
    expect(
      screen.queryByRole("button", { name: /stimmen/ }),
    ).not.toBeInTheDocument();
  });

  it("casts a vote via POST, marks it voted and re-fetches", async () => {
    fetchMock.mockResolvedValue(res(true, pollList([poll])));
    render(<PollsPanel />);
    await screen.findByText("Welches Logo?");

    fetchMock.mockResolvedValueOnce(res(true, pollList([poll]))); // refresh
    fireEvent.click(screen.getByRole("button", { name: "Für „Grün“ stimmen" }));
    await waitFor(() => expect(mockedMarkVoted).toHaveBeenCalledWith(7));
    expect(fetchMock).toHaveBeenCalledTimes(3); // GET + POST vote + GET refresh
    const voteCall = fetchMock.mock.calls[1];
    expect(voteCall[0]).toBe("/api/polls/7/vote");
    expect(voteCall[1]).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ option: 1 }),
    });
  });

  it("rejects creation with missing question or fewer than 2 options", async () => {
    fetchMock.mockResolvedValue(res(true, pollList([])));
    render(<PollsPanel />);
    await screen.findByText(/Noch keine Umfragen/);

    fireEvent.click(screen.getByRole("button", { name: "Anlegen" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Bitte Frage und mindestens 2 Optionen angeben.",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1); // only the initial GET

    fireEvent.change(screen.getByLabelText("Frage"), { target: { value: "Frage?" } });
    fireEvent.click(screen.getByRole("button", { name: "Anlegen" }));
    expect(await screen.findByText(/mindestens 2 Optionen/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1); // still no POST
  });

  it("creates a poll via JSON POST, clears the form and refreshes", async () => {
    fetchMock.mockResolvedValue(res(true, pollList([])));
    render(<PollsPanel />);
    await screen.findByText(/Noch keine Umfragen/);

    const created = { ...poll, id: 9 };
    fetchMock
      .mockResolvedValueOnce(res(true, pollList([]))) // POST /api/polls
      .mockResolvedValueOnce(res(true, pollList([created]))); // refresh
    fireEvent.change(screen.getByLabelText("Frage"), {
      target: { value: "Welches Logo?" },
    });
    fireEvent.change(screen.getByLabelText("Antwortoptionen"), {
      target: { value: "Blau\nGrün" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Anlegen" }));

    expect(await screen.findByText("Welches Logo?")).toBeInTheDocument();
    const postCall = fetchMock.mock.calls[1];
    expect(postCall[0]).toBe("/api/polls");
    expect(postCall[1]).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: "Welches Logo?",
        options: ["Blau", "Grün"],
        attachment_url: "",
      }),
    });
    expect(screen.getByLabelText("Frage")).toHaveValue("");
    expect(screen.getByLabelText("Antwortoptionen")).toHaveValue("");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("creates a poll with a file via /api/polls/upload with query params and file body", async () => {
    fetchMock.mockResolvedValue(res(true, pollList([])));
    render(<PollsPanel />);
    await screen.findByText(/Noch keine Umfragen/);

    const file = new File(["csv-data"], "ergebnis.csv", { type: "text/csv" });
    fetchMock
      .mockResolvedValueOnce(res(true, pollList([]))) // POST upload
      .mockResolvedValueOnce(res(true, pollList([]))); // refresh
    fireEvent.change(screen.getByLabelText("Frage"), {
      target: { value: "Welches Datum?" },
    });
    fireEvent.change(screen.getByLabelText("Antwortoptionen"), {
      target: { value: "Montag\nDienstag" },
    });
    fireEvent.change(screen.getByLabelText("Umfrage-Anhang-URL"), {
      target: { value: "https://cloud.example/s/x" },
    });
    fireEvent.change(screen.getByLabelText("Umfrage-Anhang"), {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByRole("button", { name: "Anlegen" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const uploadCall = fetchMock.mock.calls[1];
    expect(uploadCall[0]).toBe(
      "/api/polls/upload?question=Welches%20Datum%3F" +
        "&options=%5B%22Montag%22%2C%22Dienstag%22%5D" +
        "&filename=ergebnis.csv" +
        "&attachment_url=https%3A%2F%2Fcloud.example%2Fs%2Fx",
    );
    expect(uploadCall[1]).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "text/csv" },
    });
    expect(uploadCall[1].body).toBe(file);
  });

  it("reports failure when creating a poll fails", async () => {
    fetchMock.mockResolvedValue(res(true, pollList([])));
    render(<PollsPanel />);
    await screen.findByText(/Noch keine Umfragen/);

    fetchMock.mockResolvedValueOnce(res(false, {}, 500));
    fireEvent.change(screen.getByLabelText("Frage"), {
      target: { value: "Q?" },
    });
    fireEvent.change(screen.getByLabelText("Antwortoptionen"), {
      target: { value: "A\nB" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Anlegen" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Anlegen fehlgeschlagen: HTTP 500",
    );
    expect(screen.getByLabelText("Frage")).toHaveValue("Q?"); // kept on error
  });

  it("reports failure when voting fails", async () => {
    fetchMock.mockResolvedValue(res(true, pollList([poll])));
    render(<PollsPanel />);
    await screen.findByText("Welches Logo?");

    fetchMock.mockResolvedValueOnce(res(false, {}, 500));
    fireEvent.click(screen.getByRole("button", { name: "Für „Blau“ stimmen" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Nicht gewählt: HTTP 500",
    );
    expect(mockedMarkVoted).not.toHaveBeenCalled();
  });
});
