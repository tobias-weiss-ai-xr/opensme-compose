import type { Announcement } from "@/lib/types";

/** Operator broadcast banners (info/warn) — mirrors the legacy rendering. */
export default function AnnouncementBar({
  announcements,
}: {
  announcements: Announcement[];
}) {
  if (!announcements.length) return null;
  return (
    <div>
      {announcements.map((a, i) => (
        <div
          key={i}
          role="status"
          className={`announcement ${a.level === "warn" ? "warn" : "info"}`}
        >
          <span className="pill">⚑</span> {a.text}
        </div>
      ))}
    </div>
  );
}
