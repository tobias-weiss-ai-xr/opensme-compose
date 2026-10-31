import PortalShell from "@/components/PortalShell";
import { probeSession } from "@/lib/session";
import {
  SITE,
  buildServices,
  catalogEnvConfigured,
  loadFeedColumns,
  loadServiceSpecs,
  serverEnv,
} from "@/lib/config";
import type { Announcement, Service } from "@/lib/types";
import type { Metadata } from "next";
import { cookies } from "next/headers";

export const metadata: Metadata = {
  title: SITE.title,
  description: SITE.description,
};

// The portal data is live — never pre-render at build time.
export const dynamic = "force-dynamic";
export const revalidate = 0;

interface ApiAnnouncement {
  level?: string;
  text: string;
}

function parseAnnouncements(raw: string): Announcement[] {
  if (!raw.trim()) return [];
  try {
    const items = JSON.parse(raw) as ApiAnnouncement[];
    return (Array.isArray(items) ? items : [])
      .filter((a) => typeof a?.text === "string" && a.text.trim())
      .slice(0, 8)
      .map((a) => ({
        level: a.level === "warn" ? ("warn" as const) : ("info" as const),
        text: a.text,
      }));
  } catch {
    return [];
  }
}

async function fetchServices(): Promise<Service[]> {
  const base = serverEnv("BACKEND_URL", "http://localhost:8080");
  try {
    const res = await fetch(`${base}/api/services`, { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const data = (await res.json()) as { services: Service[] };
    if (Array.isArray(data.services) && data.services.length) return data.services;
  } catch {
    // fall through to env-based catalog
  }
  // Only fall back to the env catalog when the operator actually configured
  // catalog URLs — otherwise the live host process (BACKEND_URL only) would
  // render placeholder defaults (https://cloud.<OPENSME_DOMAIN> …) as tiles.
  return catalogEnvConfigured() ? buildServices(loadServiceSpecs()) : [];
}

export default async function HomePage() {
  const base = serverEnv("BACKEND_URL", "http://localhost:8080");
  // The Dienste tile grid is only for signed-in visitors. The backend owns
  // the session (HMAC cookie), so the server probes /api/session with the
  // request's cookies, fail-closed. Anonymous pages render no tiles at all
  // (and skip the /api/services fetch).
  const authenticated = await probeSession(base, (await cookies()).toString());

  const [services, announcements] = authenticated
    ? await Promise.all([
        fetchServices(),
        Promise.resolve(parseAnnouncements(serverEnv("PORTAL_ANNOUNCEMENTS"))),
      ])
    : [
        [] as Service[],
        parseAnnouncements(serverEnv("PORTAL_ANNOUNCEMENTS")),
      ];

  const feedColumns = loadFeedColumns();
  const aiEnabled = Boolean(serverEnv("AI_API_URL").trim());
  const pollsEnabled =
    serverEnv("POLLS_ENABLED") === "1" || serverEnv("POLLS_ENABLED") === "true";

  return (
    <PortalShell
      services={services}
      announcements={announcements}
      feedColumns={feedColumns}
      aiEnabled={aiEnabled}
      pollsEnabled={pollsEnabled}
      authenticated={authenticated}
    />
  );
}
