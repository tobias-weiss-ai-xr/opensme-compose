import { render, screen } from "@testing-library/react";
import Footer from "./Footer";
import { SITE } from "@/lib/config";

describe("Footer", () => {
  it("renders the portal name and suite copy", () => {
    render(<Footer />);
    expect(screen.getByRole("contentinfo")).toBeInTheDocument();
    expect(screen.getByText("openSME-Portal")).toBeInTheDocument();
    expect(screen.getByText(/Selbst gehostete Suite/)).toBeInTheDocument();
  });

  it("credits the configured domain and links to the project site", () => {
    render(<Footer />);
    expect(screen.getByText(SITE.domain, { exact: false })).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "opensme.graphwiz.ai" });
    expect(link).toHaveAttribute("href", "https://opensme.graphwiz.ai");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });
});
