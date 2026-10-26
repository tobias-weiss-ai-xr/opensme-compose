import { render, screen } from "@testing-library/react";
import AnnouncementBar from "./AnnouncementBar";
import type { Announcement } from "@/lib/types";

const announcements: Announcement[] = [
  { level: "info", text: "Wartung abgeschlossen" },
  { level: "warn", text: "Wartungsfenster am Sonntag" },
];

describe("AnnouncementBar", () => {
  it("renders nothing when there are no announcements", () => {
    const { container } = render(<AnnouncementBar announcements={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders one status banner per announcement with its text", () => {
    render(<AnnouncementBar announcements={announcements} />);
    expect(screen.getAllByRole("status")).toHaveLength(2);
    expect(screen.getByText("Wartung abgeschlossen")).toBeInTheDocument();
    expect(screen.getByText("Wartungsfenster am Sonntag")).toBeInTheDocument();
  });

  it("styles info and warn levels accordingly", () => {
    render(<AnnouncementBar announcements={announcements} />);
    expect(screen.getByText("Wartung abgeschlossen")).toHaveClass(
      "announcement",
      "info",
    );
    expect(screen.getByText("Wartungsfenster am Sonntag")).toHaveClass(
      "announcement",
      "warn",
    );
  });
});
