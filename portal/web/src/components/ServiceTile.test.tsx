import { render, screen } from "@testing-library/react";
import ServiceTile from "./ServiceTile";
import type { Service } from "@/lib/types";

const service: Service = {
  id: "opencloud",
  name: "OpenCloud",
  description: "Dateien in der Cloud",
  url: "https://cloud.example.com",
};

describe("ServiceTile", () => {
  it("links to the service url safely in a new tab", () => {
    render(<ServiceTile service={service} />);
    const link = screen.getByRole("link", { name: /OpenCloud/ });
    expect(link).toHaveAttribute("href", "https://cloud.example.com");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("data-pal", "OpenCloud");
  });

  it("shows the service name, description, open label and icon", () => {
    render(<ServiceTile service={service} />);
    expect(screen.getByText("OpenCloud")).toBeInTheDocument();
    expect(screen.getByText("Dateien in der Cloud")).toBeInTheDocument();
    expect(screen.getByText("Öffnen")).toBeInTheDocument();
    // serviceIcon("opencloud") from @/lib/format
    expect(screen.getByText("☁")).toBeInTheDocument();
  });

  it("keeps the legacy func-card markup contract (a > icon, h3, .product, .open)", () => {
    const { container } = render(<ServiceTile service={service} />);
    const card = container.querySelector("a.func-card");
    expect(card).not.toBeNull();
    expect(card!.querySelector("span.func-icon")).not.toBeNull();
    expect(card!.querySelector("h3")).not.toBeNull();
    expect(card!.querySelector("p.product")).not.toBeNull();
    // the Open button must remain a span.open labelled "Öffnen"
    const open = card!.querySelector("span.open");
    expect(open).not.toBeNull();
    expect(open!.textContent).toBe("Öffnen");
  });
});
