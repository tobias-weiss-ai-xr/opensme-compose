"use client";

import { useEffect, useState } from "react";
import Header from "./Header";
import Footer from "./Footer";
import ServiceTile from "./ServiceTile";
import AnnouncementBar from "./AnnouncementBar";
import LoginRequired from "./LoginRequired";
import AiCard from "./AiCard";
import ArtGallery from "./ArtGallery";
import FeedColumn from "./FeedColumn";
import IntercomPanel from "./IntercomPanel";
import PollsPanel from "./PollsPanel";
import TasksPanel from "./TasksPanel";
import QuickSwitchPalette from "./QuickSwitchPalette";
import CloudPickerProvider from "./CloudPickerProvider";
import { registerProvider } from "@/lib/bridge";
import type { SharePayload, TaskCreatePayload } from "@/lib/bridge";
import type { Announcement, Service } from "@/lib/types";
import type { FeedColumnSpec } from "@/lib/config";

interface PortalShellProps {
  services: Service[];
  announcements: Announcement[];
  feedColumns: FeedColumnSpec[];
  aiEnabled: boolean;
  pollsEnabled: boolean;
  /** Operator broadcast box in the notes panel (INTERCOM_BROADCAST=1). */
  broadcastEnabled?: boolean;
  /** True when the visitor holds a valid portal session (SSR probe, session.ts). */
  authenticated: boolean;
}

const TABS = [
  { id: "dienste", label: "Dienste" },
  { id: "umfragen", label: "Umfragen" },
  { id: "notizen", label: "Notizen" },
  { id: "aufgaben", label: "Aufgaben" },
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
  broadcastEnabled = false,
  authenticated,
}: PortalShellProps) {
  const [tab, setTab] = useState("dienste");
  const tabs = TABS.filter((t) => (t.id === "umfragen" ? pollsEnabled : true));

  // Portal-intercom: host the share-to-team slot. Sources (feed cards,
  // cloud files) never import the notes panel — they call this provider;
  // the shell owns the tab state and forwards the prefill as a prop
  // (no peer import). Registration is effect-only, so server rendering
  // and anonymous pages stay untouched.
  const [sharePrefill, setSharePrefill] = useState<SharePayload | null>(null);
  useEffect(() => {
    return registerProvider("share.open", {
      open(payload) {
        setSharePrefill(payload);
        setTab("notizen");
      },
    });
  }, []);

  // portal-tasks: host the task.create slot. Sources (feed cards, notes,
  // cloud file rows) never import the tasks panel — they call this
  // provider; the shell owns the tab state and forwards the prefill as a
  // prop. Fail-soft: with no consumer the sources render no affordance.
  const [taskPrefill, setTaskPrefill] = useState<TaskCreatePayload | null>(null);
  useEffect(() => {
    return registerProvider("task.create", {
      create(payload) {
        setTaskPrefill(payload);
        setTab("aufgaben");
      },
    });
  }, []);

  return (
    <div className="flex min-h-screen flex-col">
      {/* portal-intercom: the one shared cloud-pick provider (owner of the
          picker modal); consumers call useSlot("cloud.pick") — no peer
          imports, absent provider hides their triggers. */}
      <CloudPickerProvider />
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
          <IntercomPanel broadcastEnabled={broadcastEnabled} sharePrefill={sharePrefill} />
        </section>

        <section className="tab-panel" hidden={tab !== "aufgaben"} aria-label="Aufgaben">
          <TasksPanel taskPrefill={taskPrefill} />
        </section>

        {/* Kunstgalerie: nur für anonyme Besucher — eine angemeldete
            Sitzung sieht den Funktionskatalog, nie die Galerie. */}
        {!authenticated && <ArtGallery />}

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
