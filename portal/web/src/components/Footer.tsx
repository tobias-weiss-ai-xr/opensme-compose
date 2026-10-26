import { SITE } from "@/lib/config";

/** Portal footer — legacy footer copy + spin of the service. */
export default function Footer() {
  return (
    <footer
      className="mt-10 bg-footer text-footer-text"
      style={{ color: "var(--footer-text)" }}
    >
      <div className="mx-auto flex max-w-6xl flex-col items-center gap-2 px-5 py-6 text-sm">
        <p className="font-semibold">openSME-Portal</p>
        <p className="opacity-90">
          Selbst gehostete Suite — Cloud, Mail, Zusammenarbeit, Identität und
          Workflow-Automatisierung.
        </p>
        <p className="opacity-70 text-xs">
          {SITE.domain} · openSME · <a href="https://opensme.graphwiz.ai" target="_blank" rel="noopener noreferrer" className="underline">opensme.graphwiz.ai</a>
        </p>
      </div>
    </footer>
  );
}
