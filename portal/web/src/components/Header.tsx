"use client";

import { useEffect, useState } from "react";

import LogoutButton from "./LogoutButton";

interface Tab {
  id: string;
  label: string;
}

interface HeaderProps {
  activeTab?: string;
  onTabChange?: (tab: string) => void;
  tabs?: Tab[];
}

/**
 * Portal header — brand block, optional tabs, and a session-aware auth
 * action: "Anmelden" (→ OIDC start) while anonymous, "Abmelden" (SSO
 * end-session) once a valid portal session exists. The session is probed
 * via the public /api/session endpoint; until the probe settles the auth
 * slot is rendered empty so anonymous visitors are never offered a fake
 * logout (and signed-in users a fake login).
 */
export default function Header({ activeTab, onTabChange, tabs = [] }: HeaderProps) {
  const [authed, setAuthed] = useState<boolean | null>(null);

  useEffect(() => {
    let live = true;
    fetch("/api/session", { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (live) setAuthed(Boolean(data?.authenticated));
      })
      .catch(() => {
        if (live) setAuthed(false); // probe must never break the header
      });
    return () => {
      live = false;
    };
  }, []);

  return (
    <header className="bg-accent text-white shadow-md">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-5 py-4">
        <div>
          <h1 className="text-[26px] font-semibold tracking-wide">openSME-Portal</h1>
          <p className="text-[15px] opacity-90">
            Deine Workplace-Suite — ein Login, alle Dienste
          </p>
        </div>
        <nav className="flex flex-wrap items-center gap-3" aria-label="Bereiche">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={activeTab === t.id}
              data-tab={t.id}
              onClick={() => onTabChange?.(t.id)}
              className={`tab-btn ${activeTab === t.id ? "is-active" : ""}`}
            >
              {t.label}
            </button>
          ))}
          {authed === true ? (
            <LogoutButton />
          ) : authed === false ? (
            // OIDC start is not a Next.js page — plain GET redirect (see LoginRequired).
            // eslint-disable-next-line @next/next/no-html-link-for-pages
            <a href="/api/auth/login" className="btn-ghost btn-login">
              Anmelden
            </a>
          ) : null}
        </nav>
      </div>
    </header>
  );
}
