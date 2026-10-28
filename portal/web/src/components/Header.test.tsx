import { render, screen, fireEvent } from "@testing-library/react";
import Header from "./Header";

const tabs = [
  { id: "dienste", label: "Dienste" },
  { id: "notizen", label: "Notizen" },
];

/** Stub the /api/session probe the header uses to pick Anmelden vs Abmelden. */
function mockSession(authenticated: boolean) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () =>
        authenticated
          ? { authenticated: true, user: { sub: "s1", name: "Ada", email: "a@e" } }
          : { authenticated: false },
    }),
  );
}

describe("Header", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders brand, subtitle and Anmelden for anonymous visitors", async () => {
    mockSession(false);
    render(<Header />);
    expect(
      screen.getByRole("heading", { level: 1, name: "openSME-Portal" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Deine Workplace-Suite — ein Login, alle Dienste"),
    ).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Bereiche" })).toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    // anonymous -> login entry, never a fake logout
    expect(await screen.findByRole("link", { name: "Anmelden" })).toHaveAttribute(
      "href",
      "/api/auth/login",
    );
    expect(screen.queryByRole("button", { name: "Abmelden" })).not.toBeInTheDocument();
  });

  it("offers Abmelden only to signed-in visitors", async () => {
    mockSession(true);
    render(<Header />);
    expect(await screen.findByRole("button", { name: "Abmelden" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Anmelden" })).not.toBeInTheDocument();
  });

  it("degrades to the login entry when the probe fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<Header />);
    // failsafe: never a fake logout; anonymous view (Anmelden) is the safe default
    expect(await screen.findByRole("link", { name: "Anmelden" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Abmelden" })).not.toBeInTheDocument();
  });

  it("renders one tab button per tab with data-tab and aria-selected", async () => {
    mockSession(false);
    render(<Header activeTab="dienste" tabs={tabs} />);
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    expect(screen.getByRole("tab", { name: "Dienste" })).toHaveAttribute(
      "data-tab",
      "dienste",
    );
    expect(screen.getByRole("tab", { name: "Notizen" })).toHaveAttribute(
      "data-tab",
      "notizen",
    );
    expect(screen.getByRole("tab", { name: "Dienste" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Notizen" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("applies the is-active class only to the active tab", async () => {
    mockSession(false);
    render(<Header activeTab="notizen" tabs={tabs} />);
    expect(screen.getByRole("tab", { name: "Dienste" })).not.toHaveClass("is-active");
    expect(screen.getByRole("tab", { name: "Notizen" })).toHaveClass("tab-btn");
    expect(screen.getByRole("tab", { name: "Notizen" })).toHaveClass("is-active");
  });

  it("calls onTabChange with the tab id on click and tolerates its absence", async () => {
    mockSession(false);
    const onTabChange = vi.fn();
    const { rerender } = render(
      <Header activeTab="dienste" onTabChange={onTabChange} tabs={tabs} />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Notizen" }));
    expect(onTabChange).toHaveBeenCalledTimes(1);
    expect(onTabChange).toHaveBeenCalledWith("notizen");

    // Optional callback: clicking must not throw when onTabChange is omitted.
    rerender(<Header activeTab="dienste" tabs={tabs} />);
    fireEvent.click(screen.getByRole("tab", { name: "Dienste" }));
    expect(onTabChange).toHaveBeenCalledTimes(1);
  });
});
