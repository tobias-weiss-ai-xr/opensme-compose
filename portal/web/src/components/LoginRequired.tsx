"use client";

/**
 * Shown when an authenticated surface (intercom, polls) answers 401:
 * the visitor is not signed in. The portal keeps its public landing page,
 * but these panels require a session — offer the OIDC login entry.
 */
export default function LoginRequired({ label }: { label: string }) {
  return (
    <div className="bd-1 rounded-[10px] border border-border bg-card p-6 text-center shadow-sm">
      <p className="text-lg font-semibold">{label}</p>
      <p className="mt-2 text-sm text-dim">
        Diese Funktion ist nur für angemeldete Nutzer:innen verfügbar.
      </p>
      {/* OIDC authorize entry: a plain GET redirect to the Rust backend via
          the Next proxy — NOT a Next.js page, so a plain anchor is correct. */}
      {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
      <a
        href="/api/auth/login"
        className="btn-accent mt-4 inline-block rounded-lg px-4 py-2 text-sm font-medium"
      >
        Anmelden …
      </a>
    </div>
  );
}
