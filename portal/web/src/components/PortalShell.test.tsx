import { render, screen, fireEvent, within } from "@testing-library/react";
import PortalShell from "./PortalShell";
import type { Service, Announcement } from "@/lib/types";

// Child panels are stubbed — this suite focuses on shell wiring (tabs,
// gating, composition), not panel internals.
vi.mock("./AiCard", () => ({ default: () => <div data-testid="ai-card" /> }));
vi.mock("./IntercomPanel", () => ({ default: () => <div data-testid="intercom-panel" /> }));
vi.mock("./PollsPanel", () => ({ default: () => <div data-testid="polls-panel" /> }));
vi.mock("./FeedColumn", () => ({
  default: ({ placeholder }: { placeholder: { id: string; title: string } }) => (
    <div data-testid="feed-column-stub" data-column-id={placeholder.id} />
  ),
}));
vi.mock("./QuickSwitchPalette", () => ({
  default: ({ services }: { services: Service[] }) => (
    <div data-testid="quick-switch-stub" data-services={services.length} />
  ),
}));

const services: Service[] = [
  { id: "opencloud", name: "OpenCloud", description: "Files", url: "https://cloud.example" },
  { id: "webmail", name: "WebMail", description: "Mail", url: "https://mail.example" },
];
const announcements: Announcement[] = [{ level: "info", text: "Wartungsfenster am Sonntag" }];
const feedColumns = [
  { id: "startup", title: "Startup" },
  { id: "markt", title: "Markt" },
  { id: "legal", title: "Legal" },
];

function renderShell(overrides: Partial<Parameters<typeof PortalShell>[0]> = {}) {
  const props = {
    services,
    announcements,
    feedColumns,
    aiEnabled: false,
    pollsEnabled: false,
    ...overrides,
  };
  return render(<PortalShell {...props} />);
}

/** Query sections by label directly — hidden sections drop out of the a11y tree. */
function getSection(name: string) {
  const el = document.querySelector<HTMLElement>(`section[aria-label="${name}"]`);
  if (!el) throw new Error(`section[aria-label="${name}"] not rendered`);
  return el;
}

describe("PortalShell", () => {
  it("shows the Dienste section active by default with one tile per service", () => {
    renderShell();
    expect(getSection("Dienste")).not.toHaveAttribute("hidden");
    const tile = screen.getByRole("link", { name: /OpenCloud/ });
    expect(tile).toHaveAttribute("href", "https://cloud.example");
    expect(screen.getByRole("link", { name: /WebMail/ })).toBeInTheDocument();
    // Real AnnouncementBar is kept: banner text shows up.
    expect(screen.getByRole("status")).toHaveTextContent("Wartungsfenster am Sonntag");
    expect(screen.getByRole("tab", { name: "Dienste" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("hides the Umfragen tab and section when pollsEnabled=false, renders both when true", () => {
    const first = renderShell();
    expect(screen.queryByRole("tab", { name: "Umfragen" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("polls-panel")).not.toBeInTheDocument();

    first.unmount();
    renderShell({ pollsEnabled: true });
    expect(screen.getByRole("tab", { name: "Umfragen" })).toBeInTheDocument();
    const umfragen = getSection("Umfragen");
    expect(umfragen).toBeInTheDocument();
    expect(umfragen).toHaveAttribute("hidden"); // not the default tab
    expect(within(umfragen).getByTestId("polls-panel")).toBeInTheDocument();
  });

  it("renders the Notizen section with the IntercomPanel stub", () => {
    renderShell();
    const notizen = getSection("Notizen");
    expect(notizen).toHaveAttribute("hidden");
    expect(within(notizen).getByTestId("intercom-panel")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Notizen" })).toBeInTheDocument();
  });

  it("maps feedColumns to FeedColumn columns and gates AiCard on aiEnabled", () => {
    const first = renderShell();
    const stubs = screen.getAllByTestId("feed-column-stub");
    expect(stubs).toHaveLength(3);
    expect(stubs.map((s) => s.getAttribute("data-column-id"))).toEqual([
      "startup",
      "markt",
      "legal",
    ]);
    expect(screen.queryByTestId("ai-card")).not.toBeInTheDocument();

    first.unmount();
    renderShell({ aiEnabled: true });
    expect(screen.getByTestId("ai-card")).toBeInTheDocument();
    expect(screen.getAllByTestId("feed-column-stub")).toHaveLength(3);
  });

  it("toggles panel visibility when clicking the Umfragen tab", () => {
    renderShell({ pollsEnabled: true });
    expect(getSection("Umfragen")).toHaveAttribute("hidden");
    expect(getSection("Dienste")).not.toHaveAttribute("hidden");

    fireEvent.click(screen.getByRole("tab", { name: "Umfragen" }));
    expect(getSection("Umfragen")).not.toHaveAttribute("hidden");
    expect(getSection("Dienste")).toHaveAttribute("hidden");
    expect(screen.getByRole("tab", { name: "Umfragen" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByTestId("polls-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("intercom-panel")).toBeInTheDocument();
  });

  it("mounts the QuickSwitchPalette with the service list", () => {
    renderShell();
    expect(screen.getByTestId("quick-switch-stub")).toHaveAttribute("data-services", "2");
  });
});
