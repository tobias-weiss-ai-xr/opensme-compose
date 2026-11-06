import type { Metadata } from "next";
import Link from "next/link";
import { SITE } from "@/lib/config";

export const metadata: Metadata = {
  title: "Abgemeldet",
  robots: { index: false, follow: false },
};

/**
 * Post-logout landing page — the IdP bounces the browser here after the
 * end-session request (portal /api/logout → IdP → /logged-out). Mirrors
 * the legacy Rust-served page.
 */
export default function LoggedOutPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <div className="w-full max-w-[520px] rounded-2xl border border-border bg-card p-8 text-center shadow-sm">
        <h1 className="mb-3 text-2xl font-semibold">Du wurdest abgemeldet</h1>
        <p className="mb-4 text-dim">
          Deine Sitzung wurde auf den meisten verbundenen Diensten beendet. Die
          Sitzungen einiger Anwendungen (z.&nbsp;B. Umfragen, Archiv, Notizen)
          laufen automatisch innerhalb von 30&nbsp;Minuten ab.
        </p>
        {/* OIDC authorize entry: a plain GET redirect to the Rust backend via
            the Next proxy — NOT a Next.js page, so a plain anchor is correct. */}
        {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
        <a
          href="/api/auth/login"
          className="inline-block rounded-lg bg-accent px-8 py-3 font-semibold text-white transition hover:bg-accent-hover"
        >
          Erneut anmelden
        </a>
        <p className="mt-4">
          <Link href="/" className="text-sm text-dim underline hover:text-foreground">
            Zurück zum Portal
          </Link>
        </p>
        <p className="mt-6 text-xs text-dim">openSME-Portal · {SITE.domain}</p>
      </div>
    </div>
  );
}
