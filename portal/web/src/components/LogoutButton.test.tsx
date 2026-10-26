import { render, screen } from "@testing-library/react";
import LogoutButton from "./LogoutButton";

describe("LogoutButton", () => {
  it("submits a POST form to /api/logout", () => {
    render(<LogoutButton />);
    const button = screen.getByRole("button", { name: "Abmelden" });
    expect(button).toHaveAttribute("type", "submit");
    const form = button.closest("form");
    expect(form).not.toBeNull();
    expect(form!.getAttribute("action")).toBe("/api/logout");
    expect(form!.getAttribute("method")).toBe("post");
    expect(form!.hasAttribute("novalidate")).toBe(true);
  });

  it("passes an extra className through to the button", () => {
    render(<LogoutButton className="in-header" />);
    expect(screen.getByRole("button", { name: "Abmelden" })).toHaveClass(
      "btn-ghost",
      "btn-logout",
      "in-header",
    );
  });

  it("works without a className", () => {
    render(<LogoutButton />);
    expect(screen.getByRole("button", { name: "Abmelden" })).toHaveClass(
      "btn-ghost",
      "btn-logout",
    );
  });
});
