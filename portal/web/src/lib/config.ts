import type { Service } from "./types";

/**
 * Server-side service catalog — the values come from the same env vars the
 * Rust portal uses (docker-compose portal.environment), so both the API
 * endpoint (/api/services, Rust) and this server-rendered page stay in sync
 * with the operator's PORTAL_DOMAIN/OPENSME_DOMAIN settings.
 */
/**
 * Read a server env var at RUNTIME. Dynamic property access defeats the
 * Next.js build-time inliner (which would hard-code whatever value was
 * present — or the fallback — during `next build`).
 */
export function serverEnv(name: string, fallback = ""): string {
  return process.env[name] || fallback;
}

export function envOr(key: string, fallback: string): string {
  return serverEnv(key, fallback);
}

/**
 * True when the operator explicitly configured at least one catalog URL
 * (OPENCLOUD_URL / MAIL_URL / … ). Used to decide whether the env-based
 * catalog may serve as a fallback when the /api/services backend is down.
 * A bare default fallback without any operator intent would render bogus
 * placeholder URLs (https://cloud.<OPENSME_DOMAIN> etc.) — e.g. the live
 * host process runs with only BACKEND_URL set — so without any explicit
 * URL the fallback must stay empty.
 */
export function catalogEnvConfigured(): boolean {
  return [
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
  ].some((k) => serverEnv(k).trim() !== "");
}

const DOMAIN = envOr("PORTAL_DOMAIN", "home.opensme.graphwiz.ai");
const OPENSME = envOr("OPENSME_DOMAIN", "opensme.graphwiz.ai");

interface ServiceSpec {
  id: string;
  name: string;
  description: string;
  url: string;
}

/**
 * Catalog order + copy mirrors the Rust get_services() ordering so the
 * server-rendered tiles match /api/services exactly.
 */
export function buildServices(specs: Record<string, string>): Service[] {
  const order: ServiceSpec[] = [
    { id: "opencloud", name: "OpenCloud", description: "Dateien & Dokumente", url: specs.opencloud },
    { id: "webmail", name: "WebMail", description: "E-Mail, Kalender, Kontakte", url: specs.mail },
    { id: "workflow", name: "Workflow", description: "Prozess-Automatisierung (BPMN)", url: specs.bpm },
    { id: "invoice", name: "Invoice", description: "Rechnungen & Angebote", url: specs.invoice },
    { id: "archive", name: "Archiv", description: "Dokumentenmanagement (Paperless)", url: specs.paperless },
    { id: "chat", name: "Chat", description: "Team-Kommunikation (Matrix)", url: specs.chat },
    { id: "notes", name: "Notizen", description: "Wissens- und Dokumentseiten", url: specs.notes },
    { id: "surveys", name: "Umfragen", description: "Fragebögen & Auswertung", url: specs.surveys },
    { id: "support", name: "Support", description: "Ticket-System", url: specs.ticketing },
    { id: "website", name: "Website", description: "Öffentliche Unternehmensseite", url: specs.cms },
    { id: "shop", name: "Shop", description: "Store & Bestellungen", url: specs.shop },
  ];
  return order
    .filter((s) => s.url)
    .map(({ id, name, description, url }) => ({ id, name, description, url }));
}

export function loadServiceSpecs(): Record<string, string> {
  return {
    opencloud: envOr("OPENCLOUD_URL", `https://cloud.${OPENSME}`),
    mail: envOr("MAIL_URL", `https://webmail.${OPENSME}`),
    bpm: envOr("BPM_URL", `https://bpm.${OPENSME}`),
    invoice: envOr("INVOICE_URL", `https://invoices.${OPENSME}`),
    paperless: envOr("PAPERLESS_URL", `https://paperless.${OPENSME}`),
    chat: envOr("CHAT_URL", `https://chat.${OPENSME}`),
    notes: envOr("NOTES_URL", `https://notes.${OPENSME}`),
    surveys: envOr("SURVEYS_URL", `https://surveys.${OPENSME}`),
    ticketing: envOr("TICKETING_URL", ""),
    cms: envOr("CMS_URL", ""),
    shop: envOr("SHOP_URL", ""),
  };
}

export interface FeedColumnSpec {
  id: string;
  title: string;
}

/**
 * News-column placeholders, mirroring portal/src/feeds.rs::default_columns
 * + parse_columns: PORTAL_FEEDS JSON overrides, otherwise the German
 * defaults. Only id+title are used client-side (entries come from /api/feeds).
 */
export function loadFeedColumns(): FeedColumnSpec[] {
  const raw = serverEnv("PORTAL_FEEDS").trim();
  if (raw) {
    try {
      const parsed = JSON.parse(raw) as {
        id?: string;
        title?: string;
        urls?: string[];
      }[];
      const cols = Array.isArray(parsed)
        ? parsed
            .filter(
              (c) =>
                typeof c.id === "string" &&
                c.id.trim() &&
                typeof c.title === "string" &&
                c.title.trim() &&
                Array.isArray(c.urls) &&
                c.urls.length > 0,
            )
            .slice(0, 8)
            .map((c) => ({ id: c.id!.trim().toLowerCase(), title: c.title!.trim() }))
        : [];
      if (cols.length) return cols;
    } catch {
      // fall through to defaults on bad JSON
    }
  }
  return [
    { id: "startup", title: "Startup" },
    { id: "markt", title: "Markt" },
    { id: "legal", title: "Legal" },
  ];
}

/** Landing page metadata (matches the legacy <head>). */
export const SITE = {
  title: "openSME-Portal",
  description:
    "openSME — deine selbst gehostete Suite: Cloud, Mail, Zusammenarbeit, Identität und Workflow-Automatisierung.",
  domain: DOMAIN,
} as const;
