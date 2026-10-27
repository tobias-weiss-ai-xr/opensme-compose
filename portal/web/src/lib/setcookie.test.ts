import { describe, expect, it } from "vitest";
import { getSetCookieList, parseSetCookie } from "./setcookie";

describe("set-cookie pass-through helpers", () => {
  it("parses the Rust state cookie with its attributes", () => {
    const c = parseSetCookie(
      "OPENSMESTATE=abc123; Path=/api/auth; HttpOnly; SameSite=Lax; Secure; Max-Age=300",
    );
    expect(c.name).toBe("OPENSMESTATE");
    expect(c.value).toBe("abc123");
    expect(c.path).toBe("/api/auth");
    expect(c.httpOnly).toBe(true);
    expect(c.secure).toBe(true);
    expect(c.sameSite).toBe("lax");
    expect(c.maxAge).toBe(300);
  });

  it("preserves Max-Age=0 so logout clears actually clear", () => {
    const c = parseSetCookie("OPENSMESESSION=; Path=/; HttpOnly; Max-Age=0");
    expect(c.name).toBe("OPENSMESESSION");
    expect(c.value).toBe("");
    expect(c.maxAge).toBe(0);
    expect(c.httpOnly).toBe(true);
  });

  it("extracts every upstream set-cookie value (getSetCookie or fallback split)", () => {
    const headers = new Headers();
    headers.append("set-cookie", "OPENSMESTATE=a; Path=/api/auth; HttpOnly; Secure");
    headers.append("set-cookie", "OPENSMESESSION=; Path=/; HttpOnly; Max-Age=0");
    const list = getSetCookieList(headers);
    expect(list.length).toBe(2);
    expect(list.some((c) => c.startsWith("OPENSMESTATE=a"))).toBe(true);
    expect(list.some((c) => c.startsWith("OPENSMESESSION=; Path=/; HttpOnly; Max-Age=0"))).toBe(
      true,
    );
  });
});
