import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { buildServices, catalogEnvConfigured, envOr, loadFeedColumns, loadServiceSpecs, serverEnv } from "./config";

/**
 * Suite for the server-side service catalog (config.ts).
 *
 * config.ts bakes two module-load constants (DOMAIN, OPENSME) from env, so
 * tests that depend on those values use vi.resetModules() + a fresh dynamic
 * import after arranging process.env. Everything else (serverEnv, envOr,
 * buildServices, loadFeedColumns, loadServiceSpecs per-service overrides)
 * reads process.env at call time and works against the static import.
 */

const ENV_KEYS = [
  "PORTAL_DOMAIN",
  "OPENSME_DOMAIN",
  "PORTAL_FEEDS",
  "OPENCLOUD_URL",
  "MAIL_URL",
  "BPM_URL",
  "INVOICE_URL",
  "PAPERLESS_URL",
  "CHAT_URL",
  "NOTES_URL",
  "SURVEYS_URL",
  "TICKETING_URL",
  "CMS_URL",
  "SHOP_URL",
] as const;

const ORIG: Record<string, string | undefined> = {};

beforeAll(() => {
  for (const key of ENV_KEYS) ORIG[key] = process.env[key];
});

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (ORIG[key] === undefined) delete process.env[key];
    else process.env[key] = ORIG[key];
  }
  vi.restoreAllMocks();
});

/** Re-import config.ts so module-load constants pick up the current env. */
async function freshConfig(): Promise<typeof import("./config")> {
  vi.resetModules();
  return import("./config");
}

const FULL_SPECS: Record<string, string> = {
  opencloud: "https://cloud.example.com",
  mail: "https://webmail.example.com",
  bpm: "https://bpm.example.com",
  invoice: "https://invoices.example.com",
  paperless: "https://paperless.example.com",
  chat: "https://chat.example.com",
  notes: "https://notes.example.com",
  surveys: "https://surveys.example.com",
  ticketing: "https://support.example.com",
  cms: "https://www.example.com",
  shop: "https://shop.example.com",
};

const DEFAULT_COLUMNS = [
  { id: "startup", title: "Startup" },
  { id: "markt", title: "Markt" },
  { id: "legal", title: "Legal" },
];

describe("serverEnv", () => {
  it("returns the fallback when the var is unset (default empty string)", () => {
    delete process.env.PORTAL_FEEDS;
    expect(serverEnv("PORTAL_FEEDS")).toBe("");
    expect(serverEnv("PORTAL_FEEDS", "fallback")).toBe("fallback");
  });

  it("reads process.env at call time and tracks later changes", () => {
    process.env.PORTAL_FEEDS = "first";
    expect(serverEnv("PORTAL_FEEDS", "fallback")).toBe("first");
    process.env.PORTAL_FEEDS = "second";
    expect(serverEnv("PORTAL_FEEDS")).toBe("second");
  });

  it("envOr is an alias of serverEnv (override and fallback)", () => {
    delete process.env.MAIL_URL;
    expect(envOr("MAIL_URL", "fb")).toBe(serverEnv("MAIL_URL", "fb"));
    process.env.MAIL_URL = "https://mail.example.com";
    expect(envOr("MAIL_URL", "fb")).toBe("https://mail.example.com");
    expect(envOr("MAIL_URL", "fb")).toBe(serverEnv("MAIL_URL", "fb"));
  });
});

describe("buildServices", () => {
  it("emits all 11 tiles in the fixed catalog order with German copy", () => {
    const services = buildServices(FULL_SPECS);
    expect(services).toHaveLength(11);
    expect(services.map((s) => s.id)).toEqual([
      "opencloud",
      "webmail",
      "workflow",
      "invoice",
      "archive",
      "chat",
      "notes",
      "surveys",
      "support",
      "website",
      "shop",
    ]);
    expect(services[0]).toEqual({
      id: "opencloud",
      name: "OpenCloud",
      description: "Dateien & Dokumente",
      url: "https://cloud.example.com",
    });
    expect(services[1]).toEqual({
      id: "webmail",
      name: "WebMail",
      description: "E-Mail, Kalender, Kontakte",
      url: "https://webmail.example.com",
    });
    expect(services[10]).toEqual({
      id: "shop",
      name: "Shop",
      description: "Store & Bestellungen",
      url: "https://shop.example.com",
    });
  });

  it("filters out falsy urls (missing shop + empty ticketing/cms) -> 8 tiles", () => {
    const specs = { ...FULL_SPECS, ticketing: "", cms: "", shop: "" };
    const services = buildServices(specs);
    expect(services).toHaveLength(8);
    expect(services[0].id).toBe("opencloud");
    expect(services.map((s) => s.id)).toEqual([
      "opencloud",
      "webmail",
      "workflow",
      "invoice",
      "archive",
      "chat",
      "notes",
      "surveys",
    ]);
    expect(services.some((s) => s.id === "support" || s.id === "website" || s.id === "shop")).toBe(
      false,
    );
  });

  it("keeps website (cms) last among present when only shop is dropped", () => {
    const specs: Record<string, string> = { ...FULL_SPECS };
    delete specs.shop;
    const services = buildServices(specs);
    expect(services).toHaveLength(10);
    expect(services[services.length - 1]).toEqual({
      id: "website",
      name: "Website",
      description: "Öffentliche Unternehmensseite",
      url: "https://www.example.com",
    });
  });

  it("does not mutate its input spec map", () => {
    const specs = { ...FULL_SPECS, shop: "" };
    const snapshot = JSON.parse(JSON.stringify(specs)) as Record<string, string>;
    buildServices(specs);
    expect(specs).toEqual(snapshot);
    expect(Object.keys(specs).sort()).toEqual(Object.keys(snapshot).sort());
  });
});

describe("loadServiceSpecs", () => {
  it("returns default opensme.graphwiz.ai URLs with empty ticketing/cms/shop", async () => {
    for (const key of ENV_KEYS) delete process.env[key];
    const { loadServiceSpecs: load } = await freshConfig();
    expect(load()).toEqual({
      opencloud: "https://cloud.opensme.graphwiz.ai",
      mail: "https://webmail.opensme.graphwiz.ai",
      bpm: "https://bpm.opensme.graphwiz.ai",
      invoice: "https://invoices.opensme.graphwiz.ai",
      paperless: "https://paperless.opensme.graphwiz.ai",
      chat: "https://chat.opensme.graphwiz.ai",
      notes: "https://notes.opensme.graphwiz.ai",
      surveys: "https://surveys.opensme.graphwiz.ai",
      ticketing: "",
      cms: "",
      shop: "",
    });
  });

  it("per-service env overrides win over the derived defaults (call time)", () => {
    process.env.OPENSME_DOMAIN = "ignored-for-explicit-urls.example.com";
    process.env.INVOICE_URL = "https://billing.corp.example.com";
    const specs = loadServiceSpecs();
    expect(specs.invoice).toBe("https://billing.corp.example.com");
    expect(specs.mail).toMatch(/^https:\/\/webmail\./);
  });

  it("OPENSME_DOMAIN override re-bases every derived URL (resetModules + dynamic import)", async () => {
    process.env.OPENSME_DOMAIN = "suite.example.com";
    const { loadServiceSpecs: load } = await freshConfig();
    const specs = load();
    expect(specs.opencloud).toBe("https://cloud.suite.example.com");
    expect(specs.mail).toBe("https://webmail.suite.example.com");
    expect(specs.bpm).toBe("https://bpm.suite.example.com");
    expect(specs.paperless).toBe("https://paperless.suite.example.com");
    expect(specs.surveys).toBe("https://surveys.suite.example.com");
    expect(specs.ticketing).toBe("");
    expect(specs.cms).toBe("");
    expect(specs.shop).toBe("");
  });
});

describe("loadFeedColumns", () => {
  it("returns the German defaults when PORTAL_FEEDS is unset or whitespace-only", () => {
    delete process.env.PORTAL_FEEDS;
    expect(loadFeedColumns()).toEqual(DEFAULT_COLUMNS);
    process.env.PORTAL_FEEDS = "   \n\t ";
    expect(loadFeedColumns()).toEqual(DEFAULT_COLUMNS);
  });

  it("falls back to defaults on invalid JSON", () => {
    process.env.PORTAL_FEEDS = "{not valid json";
    expect(loadFeedColumns()).toEqual(DEFAULT_COLUMNS);
  });

  it("falls back to defaults when the JSON parses to a non-array", () => {
    process.env.PORTAL_FEEDS = JSON.stringify({ id: "a", title: "b", urls: ["https://x"] });
    expect(loadFeedColumns()).toEqual(DEFAULT_COLUMNS);
  });

  it("drops invalid entries and trims+lowercases ids (titles only trimmed)", () => {
    process.env.PORTAL_FEEDS = JSON.stringify([
      { id: "  News  ", title: "  Schlagzeilen  ", urls: ["https://a.example.com/rss"] },
      { id: "", title: "No id", urls: ["https://a"] },
      { id: "no-title", title: "   ", urls: ["https://a"] },
      { id: "no-urls", title: "Keine URLs" },
      { id: "empty-urls", title: "Leere URLs", urls: [] },
      { id: "urls-not-array", title: "URLs kein Array", urls: "https://a" },
      { id: 42, title: "Id keine Zeichenkette", urls: ["https://a"] },
    ]);
    expect(loadFeedColumns()).toEqual([{ id: "news", title: "Schlagzeilen" }]);
  });

  it("caps the column list at 8, keeping the first 8 valid entries", () => {
    process.env.PORTAL_FEEDS = JSON.stringify(
      Array.from({ length: 10 }, (_, i) => ({
        id: `col-${i}`,
        title: `Spalte ${i}`,
        urls: ["https://feeds.example.com"],
      })),
    );
    const cols = loadFeedColumns();
    expect(cols).toHaveLength(8);
    expect(cols[0].id).toBe("col-0");
    expect(cols[7].id).toBe("col-7");
    expect(cols.some((c) => c.id === "col-8" || c.id === "col-9")).toBe(false);
  });

  it("falls back to defaults when no entry survives filtering", () => {
    process.env.PORTAL_FEEDS = JSON.stringify([
      { id: "a", title: "Ohne URLs", urls: [] },
      { id: "", title: "Ohne Id", urls: ["https://a"] },
    ]);
    expect(loadFeedColumns()).toEqual(DEFAULT_COLUMNS);
  });
});

describe("SITE", () => {
  it("exposes the fixed title/description and the default domain", async () => {
    delete process.env.PORTAL_DOMAIN;
    const { SITE } = await freshConfig();
    expect(SITE.title).toBe("openSME-Portal");
    expect(SITE.description).toBe(
      "openSME — deine selbst gehostete Suite: Cloud, Mail, Zusammenarbeit, Identität und Workflow-Automatisierung.",
    );
    expect(SITE.domain).toBe("home.opensme.graphwiz.ai");
  });

  it("bakes PORTAL_DOMAIN into SITE.domain at module load", async () => {
    process.env.PORTAL_DOMAIN = "portal.corp.example.com";
    const { SITE } = await freshConfig();
    expect(SITE.domain).toBe("portal.corp.example.com");
    expect(SITE.title).toBe("openSME-Portal");
  });
});

describe("catalogEnvConfigured", () => {
  it("is false when no catalog URL vars are set (live host process env)", () => {
    for (const key of [
      "OPENCLOUD_URL",
      "MAIL_URL",
      "BPM_URL",
      "INVOICE_URL",
      "PAPERLESS_URL",
      "CHAT_URL",
      "NOTES_URL",
      "SURVEYS_URL",
      "TICKETING_URL",
      "CMS_URL",
      "SHOP_URL",
    ]) {
      delete process.env[key];
    }
    expect(catalogEnvConfigured()).toBe(false);
  });

  it("is false for whitespace-only values", () => {
    delete process.env.PAPERLESS_URL;
    process.env.OPENCLOUD_URL = "   ";
    expect(catalogEnvConfigured()).toBe(false);
  });

  it("is true when at least one catalog URL is explicitly configured", () => {
    delete process.env.OPENCLOUD_URL;
    process.env.PAPERLESS_URL = "https://paperless.example";
    expect(catalogEnvConfigured()).toBe(true);
  });
});
