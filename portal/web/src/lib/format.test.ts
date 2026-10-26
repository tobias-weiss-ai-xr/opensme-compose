import {
  formatDate,
  humanSize,
  serviceIcon,
  feedMetaLabel,
  escapeHtml,
  filterServices,
} from "./format";

describe("format helpers (legacy app.js parity)", () => {
  it("humanSize formats B/KB/MB", () => {
    expect(humanSize(512)).toBe("512 B");
    expect(humanSize(12_345)).toBe("12.1 KB");
    expect(humanSize(1.5 * 1024 * 1024)).toBe("1.5 MB");
    expect(humanSize(null)).toBe("");
    expect(humanSize(undefined)).toBe("");
  });

  it("formatDate parses ISO dates to de-DE dd.MM.yyyy and tolerates garbage", () => {
    expect(formatDate("2025-01-01T00:00:00+00:00")).toBe("01.01.2025");
    expect(formatDate(null)).toBe("");
    expect(formatDate("not-a-date")).toBe("");
  });

  it("feedMetaLabel combines date + source like app.js", () => {
    expect(feedMetaLabel({ date: "2025-01-01T00:00:00Z", source: "t3n.de" })).toBe(
      "01.01.2025 · t3n.de",
    );
    expect(feedMetaLabel({ source: "lto.de" })).toBe("lto.de");
    expect(feedMetaLabel({ date: "2025-01-01T00:00:00Z" })).toBe("01.01.2025");
  });

  it("serviceIcon maps known ids and falls back", () => {
    expect(serviceIcon("opencloud")).toBe("☁");
    expect(serviceIcon("invoice")).toBe("🧾");
    expect(serviceIcon("unknown")).toBe("🧩");
  });

  it("escapeHtml neutralizes markup (never rendered via innerHTML anyway)", () => {
    expect(escapeHtml(`<script>alert("x")</script>'`)).toBe(
      "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&#x27;",
    );
  });

  it("filterServices is case-insensitive substring match; empty query returns all", () => {
    const svcs = [
      { name: "OpenCloud" },
      { name: "WebMail" },
      { name: "Workflow" },
    ];
    expect(filterServices(svcs, "open").map((s) => s.name)).toEqual(["OpenCloud"]);
    expect(filterServices(svcs, "  ")).toHaveLength(3);
    expect(filterServices(svcs, "zzz")).toHaveLength(0);
  });
});
