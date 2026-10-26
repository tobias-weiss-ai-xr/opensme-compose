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
        <Link
          href="/"
          className="inline-block rounded-lg bg-accent px-8 py-3 font-semibold text-white transition hover:bg-accent-hover"
        >
          Zurück zum Portal
        </Link>
        <p className="mt-6 text-xs text-dim">openSME-Portal · {SITE.domain}</p>
      </div>
    </div>
  );
}
