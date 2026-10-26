import { render, screen, fireEvent } from "@testing-library/react";
import Header from "./Header";

const tabs = [
  { id: "dienste", label: "Dienste" },
  { id: "notizen", label: "Notizen" },
];

describe("Header", () => {
  it("renders brand, subtitle and the logout button even without tabs", () => {
    render(<Header />);
    expect(
      screen.getByRole("heading", { level: 1, name: "openSME-Portal" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Deine Workplace-Suite — ein Login, alle Dienste"),
    ).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Bereiche" })).toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Abmelden" })).toBeInTheDocument();
  });

  it("renders one tab button per tab with data-tab and aria-selected", () => {
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

  it("applies the is-active class only to the active tab", () => {
    render(<Header activeTab="notizen" tabs={tabs} />);
    expect(screen.getByRole("tab", { name: "Dienste" })).not.toHaveClass("is-active");
    expect(screen.getByRole("tab", { name: "Notizen" })).toHaveClass("tab-btn");
    expect(screen.getByRole("tab", { name: "Notizen" })).toHaveClass("is-active");
  });

  it("calls onTabChange with the tab id on click and tolerates its absence", () => {
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
