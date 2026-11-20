import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import TasksPanel from "./TasksPanel";
import {
  createTask,
  deleteTask,
  fetchTasks,
  fetchTaskUsers,
  patchTask,
} from "@/lib/api";
import type { TeamTask, TaskListResponse } from "@/lib/types";

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
  fetchTasks: vi.fn(),
  fetchTaskUsers: vi.fn(),
  createTask: vi.fn(),
  patchTask: vi.fn(),
  deleteTask: vi.fn(),
  AuthError: MockAuthError,
}));

const AuthError = MockAuthError;
const mockedFetch = vi.mocked(fetchTasks);
const mockedUsers = vi.mocked(fetchTaskUsers);
const mockedCreate = vi.mocked(createTask);
const mockedPatch = vi.mocked(patchTask);
const mockedDelete = vi.mocked(deleteTask);

/** Settle helper: lets the mount effects (load + users) flush. */
async function settle() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 80));
  });
}

function task(partial: Partial<TeamTask> & { id: number; title: string }): TeamTask {
  return {
    assignee: "bob@firma.de",
    author: "anna@firma.de",
    priority: "normal",
    status: "open",
    ...partial,
  };
}

function listResponse(tasks: TeamTask[], latest = tasks.length): TaskListResponse {
  return { tasks, latest };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedFetch.mockResolvedValue(listResponse([]));
  mockedUsers.mockResolvedValue({ users: ["anna@firma.de", "bob@firma.de"] });
});

describe("TasksPanel", () => {
  it("loads tasks and known users on mount and renders them sorted", async () => {
    mockedFetch.mockResolvedValue(
      listResponse([
        task({ id: 1, title: "Alt", due: "2026-01-01" }),
        task({ id: 3, title: "Hoch", priority: "high", due: "2026-01-01" }),
        task({ id: 2, title: "Erledigt", status: "done" }),
      ]),
    );
    const { container } = render(<TasksPanel />);
    await settle();
    // open first (prio high before normal), done last
    const titles = Array.from(container.querySelectorAll(".ts-title")).map(
      (e) => e.textContent,
    );
    expect(titles).toEqual(["Hoch", "Alt", "Erledigt"]);
    expect(mockedFetch).toHaveBeenCalledWith(undefined);
    expect(mockedUsers).toHaveBeenCalledOnce();
    expect(screen.getByPlaceholderText("Zuständig (Name oder E-Mail)")).toBeTruthy();
  });

  it("creates a task through the form and prepends it", async () => {
    const created = task({ id: 9, title: "Neu", assignee: "bob@firma.de" });
    mockedCreate.mockResolvedValue(created);
    render(<TasksPanel />);
    await settle();
    fireEvent.change(screen.getByPlaceholderText("Titel der Aufgabe"), {
      target: { value: "Neu" },
    });
    fireEvent.change(screen.getByPlaceholderText("Zuständig (Name oder E-Mail)"), {
      target: { value: "bob@firma.de" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Aufgabe anlegen" }));
    await waitFor(() => expect(mockedCreate).toHaveBeenCalledOnce());
    expect(mockedCreate.mock.calls[0][0]).toMatchObject({
      title: "Neu",
      assignee: "bob@firma.de",
      priority: "normal",
    });
    await waitFor(() => expect(screen.getByText("Neu")).toBeTruthy());
    // form resets after success
    expect((screen.getByPlaceholderText("Titel der Aufgabe") as HTMLInputElement).value).toBe("");
  });

  it("keeps the form and shows the server error inline on failure (e.g. 403)", async () => {
    mockedCreate.mockRejectedValue(new Error("nur Zuständiger oder Autor darf Aufgaben abschließen oder löschen"));
    render(<TasksPanel />);
    await settle();
    fireEvent.change(screen.getByPlaceholderText("Titel der Aufgabe"), {
      target: { value: "Bleibt" },
    });
    fireEvent.change(screen.getByPlaceholderText("Zuständig (Name oder E-Mail)"), {
      target: { value: "bob@firma.de" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Aufgabe anlegen" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("nur Zuständiger"));
    // form preserved
    expect((screen.getByPlaceholderText("Titel der Aufgabe") as HTMLInputElement).value).toBe("Bleibt");
  });

  it("switches filters and refetches with the filter param", async () => {
    render(<TasksPanel />);
    await settle();
    mockedFetch.mockResolvedValue(listResponse([task({ id: 5, title: "Mir", assignee: "ich@firma.de" })]));
    fireEvent.click(screen.getByRole("button", { name: "Meine" }));
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledWith("mine"));
    await waitFor(() => expect(screen.getByText("Mir")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Erledigt" }));
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledWith("done"));
  });

  it("toggles done and deletes via the API, surfacing 403 inline", async () => {
    const t = task({ id: 4, title: "Wichtig" });
    mockedFetch.mockResolvedValue(listResponse([t]));
    render(<TasksPanel />);
    await settle();
    await waitFor(() => expect(screen.getByText("Wichtig")).toBeTruthy());

    mockedPatch.mockResolvedValue({ ...t, status: "done" });
    fireEvent.click(screen.getByLabelText(/als erledigt markieren/));
    await waitFor(() => expect(mockedPatch).toHaveBeenCalledWith(4, { status: "done" }));
    await waitFor(() => expect(screen.getByText("Wichtig").className).toContain("ts-title"));

    mockedDelete.mockRejectedValue(new Error("nur Zuständiger oder Autor darf Aufgaben abschließen oder löschen"));
    fireEvent.click(screen.getByLabelText("„Wichtig“ löschen"));
    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith(4));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("nur Zuständiger"));
    // task still listed — delete failed
    expect(screen.getByText("Wichtig")).toBeTruthy();
  });

  it("marks overdue open tasks and shows the source link", async () => {
    mockedFetch.mockResolvedValue(
      listResponse([
        task({ id: 7, title: "Überfällig", due: "2020-01-01", source_url: "https://news.example/a" }),
      ]),
    );
    render(<TasksPanel />);
    await settle();
    await waitFor(() => expect(screen.getByText(/überfällig/)).toBeTruthy());
    const link = screen.getByText("Quelle öffnen");
    expect(link.getAttribute("href")).toBe("https://news.example/a");
  });

  it("applies the task.create prefill from the shell", async () => {
    const { rerender } = render(<TasksPanel taskPrefill={null} />);
    await settle();
    rerender(<TasksPanel taskPrefill={{ title: "Feed-Beitrag", url: "https://news.example/a" }} />);
    await waitFor(() =>
      expect((screen.getByPlaceholderText("Titel der Aufgabe") as HTMLInputElement).value).toBe(
        "Feed-Beitrag",
      ),
    );
    expect((screen.getByPlaceholderText("Quellen-Link (https://…)") as HTMLInputElement).value).toBe(
      "https://news.example/a",
    );
  });

  it("shows the login CTA when the backend answers 401", async () => {
    mockedFetch.mockRejectedValue(new AuthError());
    render(<TasksPanel />);
    await settle();
    await waitFor(() => expect(screen.getByText(/nur für angemeldete/i)).toBeTruthy());
  });

  it("polls the delta endpoint with ?since= cursor", async () => {
    vi.useFakeTimers();
    try {
      mockedFetch.mockResolvedValue(listResponse([task({ id: 1, title: "Eins" })], 1));
      render(<TasksPanel />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(mockedFetch).toHaveBeenCalledWith(undefined);
      mockedFetch.mockResolvedValue(listResponse([task({ id: 2, title: "Zwei" })], 2));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(7000);
      });
      expect(mockedFetch).toHaveBeenCalledWith(undefined, 1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByText("Zwei")).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});
