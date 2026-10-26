"use client";

import { useEffect, useState } from "react";
import { feedMetaLabel } from "@/lib/format";
import { fetchFeeds } from "@/lib/api";
import type { FeedColumnPayload } from "@/lib/types";

interface FeedColumnProps {
  placeholder: { id: string; title: string };
}

const REFRESH_MS = 10 * 60 * 1000; // 10 min, matching legacy app.js

/**
 * One news column (Startup / Markt / Legal). The Rust backend aggregates
 * RSS/Atom server-side (/api/feeds, same-origin via rewrite). Entries are
 * rendered as React text nodes — feed titles are untrusted input.
 */
export default function FeedColumn({ placeholder }: FeedColumnProps) {
  const [column, setColumn] = useState<FeedColumnPayload | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const data = await fetchFeeds();
        if (!alive) return;
        const col = data.columns.find((c) => c.id === placeholder.id);
        setColumn(col ?? { id: placeholder.id, title: placeholder.title, entries: [] });
        setError(false);
      } catch {
        if (alive) setError(true);
      }
    }
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [placeholder.id, placeholder.title]);

  const entries = column?.entries ?? [];

  return (
    <div className="feed-card" data-feed-column={placeholder.id}>
      <h2>{placeholder.title}</h2>
      {error && <ul><li className="error">Feed derzeit nicht verfügbar</li></ul>}
      {!error && entries.length === 0 && (
        <ul><li className="loading">Lade Meldungen…</li></ul>
      )}
      {!error && entries.length > 0 && (
        <ul aria-live="polite">
          {entries.map((e, i) => (
            <li key={`${e.url}-${i}`}>
              <a href={e.url} target="_blank" rel="noopener noreferrer">
                {e.title}
              </a>
              <span className="date">{feedMetaLabel(e)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
