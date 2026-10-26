/**
 * Formatting helpers — behavior mirrors the legacy portal/app.js so the
 * rendered output stays byte-compatible in label style.
 */

/** Human-readable byte size: "512 B" / "12.3 KB" / "1.2 MB". */
export function humanSize(bytes: number | null | undefined): string {
  if (bytes == null || Number.isNaN(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** ISO-8601 → "dd.MM.yyyy" (de-DE), matching app.js toLocaleDateString. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
}

/** "dd.MM.yyyy · source" or just "source" — as rendered by app.js. */
export function feedMetaLabel(entry: { date?: string | null; source?: string }): string {
  const date = formatDate(entry.date);
  if (entry.source) return date ? `${date} · ${entry.source}` : entry.source;
  return date;
}

/** Emoji glyph for a service card (decorative, never user-controlled). */
export function serviceIcon(id: string): string {
  switch (id) {
    case "opencloud":
      return "☁";
    case "webmail":
      return "✉";
    case "support":
      return "🆘";
    case "website":
      return "🌐";
    case "shop":
      return "🛒";
    case "workflow":
      return "⚙";
    case "invoice":
      return "🧾";
    case "archive":
      return "🗄";
    case "chat":
      return "🗨";
    case "notes":
      return "📋";
    case "surveys":
      return "🗳";
    default:
      return "🧩";
  }
}

/** Escape user-controlled text for HTML contexts (defense in depth). */
export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

/** Simple fuzzy filter over service names (quick-switch palette). */
export function filterServices<T extends { name: string }>(
  services: T[],
  query: string,
): T[] {
  const q = query.trim().toLowerCase();
  if (!q) return services;
  return services.filter((s) => s.name.toLowerCase().includes(q));
}

const VOTED_KEY = "opensme-poll-voted";

/** Poll "already voted" bookkeeping in localStorage (per-browser only). */
export function votedSet(): Record<string, boolean> {
  try {
    return JSON.parse(window.localStorage.getItem(VOTED_KEY) || "{}") || {};
  } catch {
    return {};
  }
}

export function markVoted(id: number): void {
  const v = votedSet();
  v[String(id)] = true;
  try {
    window.localStorage.setItem(VOTED_KEY, JSON.stringify(v));
  } catch {
    // localStorage unavailable (private mode) — voting still works per session
  }
}
