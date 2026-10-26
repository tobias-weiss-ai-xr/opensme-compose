import { render, screen, fireEvent } from "@testing-library/react";
import QuickSwitchPalette from "./QuickSwitchPalette";
import ServiceTile from "./ServiceTile";
import type { Service } from "@/lib/types";

const services: Service[] = [
  { id: "opencloud", name: "OpenCloud", description: "Files", url: "https://cloud.example" },
  { id: "webmail", name: "WebMail", description: "Mail", url: "https://mail.example" },
];

describe("QuickSwitchPalette", () => {
  it("filters services as the user types", () => {
    render(<QuickSwitchPalette services={services} />);
    fireEvent.click(screen.getByLabelText("Quick switch (Ctrl+K)"));
    const input = screen.getByLabelText("Jump to");
    fireEvent.change(input, { target: { value: "mail" } });
    expect(screen.getByText("WebMail")).toBeInTheDocument();
    expect(screen.queryByText("OpenCloud")).not.toBeInTheDocument();
  });

  it("shows 'no match' for an unmatched query", () => {
    render(<QuickSwitchPalette services={services} />);
    fireEvent.click(screen.getByLabelText("Quick switch (Ctrl+K)"));
    fireEvent.change(screen.getByLabelText("Jump to"), {
      target: { value: "zzz" },
    });
    expect(screen.getByText("no match")).toBeInTheDocument();
  });

  it("opens the selected service in a new tab", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<QuickSwitchPalette services={services} />);
    fireEvent.click(screen.getByLabelText("Quick switch (Ctrl+K)"));
    fireEvent.click(screen.getByText("OpenCloud"));
    expect(open).toHaveBeenCalledWith("https://cloud.example", "_blank", "noopener");
    open.mockRestore();
  });
});

describe("ServiceTile", () => {
  it("links out safely and carries the palette hook", () => {
    render(<ServiceTile service={services[0]} />);
    const link = screen.getByRole("link", { name: /OpenCloud/ });
    expect(link).toHaveAttribute("href", "https://cloud.example");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("data-pal", "OpenCloud");
  });
});

describe("QuickSwitchPalette ctrl+k shortcut", () => {
  it("toggles the dialog on Ctrl+K", () => {
    render(<QuickSwitchPalette services={services} />);
    expect(screen.queryByLabelText("Jump to")).not.toBeInTheDocument();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    expect(screen.getByLabelText("Jump to")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    expect(screen.queryByLabelText("Jump to")).not.toBeInTheDocument();
  });
});
