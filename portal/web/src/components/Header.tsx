"use client";

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

/** Portal header — brand block, optional tabs, logout (SSO end-session). */
export default function Header({ activeTab, onTabChange, tabs = [] }: HeaderProps) {
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
          <LogoutButton />
        </nav>
      </div>
    </header>
  );
}
