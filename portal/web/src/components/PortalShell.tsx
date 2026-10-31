"use client";

import { useState } from "react";
import Header from "./Header";
import Footer from "./Footer";
import ServiceTile from "./ServiceTile";
import AnnouncementBar from "./AnnouncementBar";
import LoginRequired from "./LoginRequired";
import AiCard from "./AiCard";
import FeedColumn from "./FeedColumn";
import IntercomPanel from "./IntercomPanel";
import PollsPanel from "./PollsPanel";
import QuickSwitchPalette from "./QuickSwitchPalette";
import type { Announcement, Service } from "@/lib/types";
import type { FeedColumnSpec } from "@/lib/config";

interface PortalShellProps {
  services: Service[];
  announcements: Announcement[];
  feedColumns: FeedColumnSpec[];
  aiEnabled: boolean;
  pollsEnabled: boolean;
  /** True when the visitor holds a valid portal session (SSR probe, session.ts). */
  authenticated: boolean;
}

const TABS = [
  { id: "dienste", label: "Dienste" },
  { id: "umfragen", label: "Umfragen" },
  { id: "notizen", label: "Notizen" },
];

/**
 * Client shell: tabs + interactive panels. Server-rendered content
 * (services, announcements, feed cards, AI box) is passed in from page.tsx
 * so the landing page is meaningful without JS — same progressive
 * enhancement as the legacy portal.
 */
export default function PortalShell({
  services,
  announcements,
  feedColumns,
  aiEnabled,
  pollsEnabled,
  authenticated,
}: PortalShellProps) {
  const [tab, setTab] = useState("dienste");
  const tabs = TABS.filter((t) => (t.id === "umfragen" ? pollsEnabled : true));

  return (
    <div className="flex min-h-screen flex-col">
      <AnnouncementBar announcements={announcements} />
      <Header activeTab={tab} onTabChange={setTab} tabs={tabs} />
      <main className="mx-auto w-full max-w-6xl flex-1 px-5 py-6">
        <section className="tab-panel" hidden={tab !== "dienste"} aria-label="Dienste">
          <h2 className="mb-4 text-xl font-bold">Dienste</h2>
          {authenticated ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {services.map((s) => (
                <ServiceTile key={s.id} service={s} />
              ))}
              {aiEnabled && (
                <div className="func-card w-full sm:w-auto" style={{ gridColumn: "span 1" }}>
                  <AiCard />
                </div>
              )}
            </div>
          ) : (
            <LoginRequired label="Dienste" />
          )}
        </section>

        {pollsEnabled && (
          <section className="tab-panel" hidden={tab !== "umfragen"} aria-label="Umfragen">
            <PollsPanel />
          </section>
        )}

        <section className="tab-panel" hidden={tab !== "notizen"} aria-label="Notizen">
          <IntercomPanel />
        </section>

        <section className="mt-10" aria-label="News">
          <h2 className="mb-4 text-xl font-bold">News</h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {feedColumns.map((c) => (
              <FeedColumn key={c.id} placeholder={c} />
            ))}
          </div>
        </section>
      </main>
      <Footer />
      {authenticated && <QuickSwitchPalette services={services} />}
    </div>
  );
}
