import { describe, expect, it } from "vitest";
import { buildCsp, idpOriginFromUrl } from "./csp";

/**
 * CSP contract for the logout flow.
 *
 * The logout button is a native form POST /api/logout (disabled JS).
 * The Rust handler answers 303 to the IdP end-session URL, i.e. the form
 * submission navigates CROSS-ORIGIN. Chrome enforces `form-action` on
 * redirects of form submissions, so `form-action 'self'` alone blocks the
 * follow and the user stays on the portal (silent logout breakage,
 * found live 2026-11-19 while verifying the R20 gallery). The IdP origin
 * (IDP_URL) must therefore be allow-listed in form-action.
 */

describe("idpOriginFromUrl", () => {
  it("normalizes a full IdP URL to its origin", () => {
    expect(idpOriginFromUrl("https://auth.home.opensme.graphwiz.ai")).toBe(
      "https://auth.home.opensme.graphwiz.ai",
    );
    expect(
      idpOriginFromUrl("https://auth.example.com/oidc/v1/end_session?x=1"),
    ).toBe("https://auth.example.com");
  });

  it("accepts a bare domain (no scheme) as https", () => {
    expect(idpOriginFromUrl("auth.home.opensme.graphwiz.ai")).toBe(
      "https://auth.home.opensme.graphwiz.ai",
    );
  });

  it("returns empty for unset/invalid input", () => {
    expect(idpOriginFromUrl(undefined)).toBe("");
    expect(idpOriginFromUrl("")).toBe("");
    expect(idpOriginFromUrl("   ")).toBe("");
    expect(idpOriginFromUrl("::not-a-url::")).toBe("");
  });
});

describe("buildCsp", () => {
  it("keeps form-action 'self' when no IdP origin is configured", () => {
    const csp = buildCsp("abc123");
    expect(csp).toContain("form-action 'self'");
    expect(csp).not.toContain("https://");
  });

  it("allow-lists the IdP origin in form-action (logout redirect hop)", () => {
    const csp = buildCsp("abc123", "https://auth.home.opensme.graphwiz.ai");
    expect(csp).toContain(
      "form-action 'self' https://auth.home.opensme.graphwiz.ai",
    );
  });

  it("carries the nonce in script-src and keeps the strict baseline", () => {
    const csp = buildCsp("n0nce");
    const script = csp.split("; ").find((d) => d.startsWith("script-src"));
    expect(script).toContain("'nonce-n0nce'");
    expect(script).not.toContain("'unsafe-inline'");
    expect(script).not.toContain("'unsafe-eval'");
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("connect-src 'self'");
    expect(csp).toContain("frame-src 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("base-uri 'self'");
  });
});
