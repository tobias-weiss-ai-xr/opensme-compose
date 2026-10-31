import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import PortalShell from "./PortalShell";
import type { Service } from "@/lib/types";

const services: Service[] = [
  {
    id: "opencloud",
    name: "OpenCloud",
    description: "Cloud storage",
    url: "https://cloud.example",
  },
  {
    id: "webmail",
    name: "Webmail",
    description: "Email",
    url: "https://webmail.example",
  },
];

const base = {
  services,
  announcements: [{ level: "info" as const, text: "Willkommen" }],
  feedColumns: [{ id: "startup", title: "Startup" }],
  aiEnabled: false,
  pollsEnabled: true,
};

/**
 * Stub the same-origin API. /api/session drives the header probe (Anmelden
 * vs Abmelden); the interactive panels (intercom/polls/feeds/AI) must never
 * crash the shell — anonymous requests answer 401 (their LoginRequired
 * state), authenticated requests answer an empty body.
 */
function stubFetch(authenticated: boolean) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/session")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () =>
            authenticated
              ? {
                  authenticated: true,
                  user: { sub: "s1", name: "Ada", email: "a@e" },
                }
              : { authenticated: false },
        });
      }
      return Promise.resolve(
        authenticated
          ? { ok: true, status: 200, json: async () => ({}) }
          : { ok: false, status: 401, json: async () => ({}) },
      );
    }),
  );
}

describe("PortalShell — Dienste tiles only for signed-in visitors", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("hides the service tiles and offers login for anonymous visitors", () => {
    stubFetch(false);
    render(<PortalShell {...base} authenticated={false} />);

    // public chrome still renders (announcement, heading, news)
    expect(screen.getByText("Willkommen")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Dienste" }),
    ).toBeInTheDocument();

    // no service tile anywhere on the anonymous page
    expect(screen.queryByText("OpenCloud")).not.toBeInTheDocument();
    expect(screen.queryByText("Webmail")).not.toBeInTheDocument();

    // login prompt with the OIDC entry instead of the grid
    expect(
      screen.getAllByText("Diese Funktion ist nur für angemeldete Nutzer:innen verfügbar.")
        .length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByRole("link", { name: /Anmelden/ }).length,
    ).toBeGreaterThan(0);

    // no quick-switch launcher over the (hidden) catalog
    expect(
      screen.queryByLabelText("Quick switch (Ctrl+K)"),
    ).not.toBeInTheDocument();

    // News stay public for visitors
    expect(screen.getByRole("heading", { name: "News" })).toBeInTheDocument();
  });

  it("renders the service tiles for signed-in visitors", () => {
    stubFetch(true);
    render(<PortalShell {...base} authenticated={true} />);

    expect(screen.getByText("OpenCloud")).toBeInTheDocument();
    expect(screen.getByText("Webmail")).toBeInTheDocument();

    const tile = screen.getByRole("link", { name: /OpenCloud/ });
    expect(tile).toHaveAttribute("href", "https://cloud.example");
    expect(tile).toHaveAttribute("target", "_blank");

    expect(screen.getAllByText("Öffnen").length).toBeGreaterThanOrEqual(2);
    // quick-switch launcher available to signed-in users
    expect(
      screen.getByLabelText("Quick switch (Ctrl+K)"),
    ).toBeInTheDocument();

    // no login prompt while signed in
    expect(
      screen.queryByText("Diese Funktion ist nur für angemeldete Nutzer:innen verfügbar."),
    ).not.toBeInTheDocument();
  });

  it("keeps the Umfragen tab only when polls are enabled", () => {
    stubFetch(true);
    render(<PortalShell {...base} authenticated={true} pollsEnabled={false} />);
    expect(screen.queryByRole("tab", { name: "Umfragen" })).not.toBeInTheDocument();
  });
});
