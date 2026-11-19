import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import LoggedOutPage from "./page";

describe("LoggedOutPage", () => {
  it("offers a link back to the OIDC login entry after logout", () => {
    render(<LoggedOutPage />);
    // The login entry is a plain GET to the Next proxy → Rust OIDC authorize.
    expect(screen.getByRole("link", { name: /anmelden/i })).toHaveAttribute(
      "href",
      "/api/auth/login",
    );
  });

  it("still offers the portal landing link", () => {
    render(<LoggedOutPage />);
    expect(
      screen.getByRole("link", { name: /zurück zum portal/i }),
    ).toHaveAttribute("href", "/");
  });

  it("shows the art gallery below the sign-out card", () => {
    render(<LoggedOutPage />);
    expect(screen.getByTestId("art-gallery")).toBeInTheDocument();
    // die Karte bleibt Primärinhalt: Login-Einstieg unverändert über der
    // Galerie im DOM
    const login = screen.getByRole("link", { name: /anmelden/i });
    const gallery = screen.getByTestId("art-gallery");
    expect(login.compareDocumentPosition(gallery) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
