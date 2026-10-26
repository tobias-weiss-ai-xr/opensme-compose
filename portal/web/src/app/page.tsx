import PortalShell from "@/components/PortalShell";
import {
  SITE,
  buildServices,
  loadFeedColumns,
  loadServiceSpecs,
  serverEnv,
} from "@/lib/config";
import type { Announcement, Service } from "@/lib/types";
import type { Metadata } from "next";

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
  return buildServices(loadServiceSpecs());
}

export default async function HomePage() {
  const [services, announcements] = await Promise.all([
    fetchServices(),
    Promise.resolve(parseAnnouncements(serverEnv("PORTAL_ANNOUNCEMENTS"))),
  ]);

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
    />
  );
}
