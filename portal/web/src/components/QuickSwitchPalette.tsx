"use client";

import { useEffect, useRef, useState } from "react";
import { filterServices } from "@/lib/format";
import type { Service } from "@/lib/types";

interface QuickSwitchPaletteProps {
  services: Service[];
}

/**
 * Ctrl/Cmd+K quick-switch palette over the service catalog — same behavior
 * as the legacy portal's app.js (substring filter + keyboard nav).
 */
export default function QuickSwitchPalette({ services }: QuickSwitchPaletteProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const hits = filterServices(services, query);
  const safeSelected = hits.length ? Math.min(selected, hits.length - 1) : 0;

  function close() {
    setOpen(false);
    setQuery("");
    setSelected(0);
  }

  function openService(s: Service) {
    window.open(s.url, "_blank", "noopener");
    close();
  }

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
        return;
      }
      if (!open) return;
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!hitsRef.current.length) return;
        const dir = e.key === "ArrowDown" ? 1 : -1;
        setSelected((s) => (s + dir + hitsRef.current.length) % hitsRef.current.length);
      } else if (e.key === "Enter") {
        e.preventDefault();
        const hit = hitsRef.current[safeSelectedRef.current];
        if (hit) openService(hit);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // keep refs in sync for the document-level handler
  const hitsRef = useRef(hits);
  hitsRef.current = hits;
  const safeSelectedRef = useRef(safeSelected);
  safeSelectedRef.current = safeSelected;

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  return (
    <>
      <button
        type="button"
        title="Quick switch (Ctrl+K)"
        aria-label="Quick switch (Ctrl+K)"
        className="fixed right-5 bottom-5 z-50 h-12 w-12 rounded-full bg-accent text-white text-lg font-bold shadow-lg transition hover:bg-accent-hover"
        onClick={() => setOpen(true)}
      >
        ⌘K
      </button>
      {open && (
        <div
          className="fixed inset-0 z-40 flex items-start justify-center bg-black/40 p-6 pt-24"
          onClick={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div
            role="dialog"
            aria-label="Quick switch"
            className="w-full max-w-md rounded-xl border border-border bg-card shadow-xl"
          >
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setSelected(0);
              }}
              onKeyDown={(e) => e.stopPropagation()}
              placeholder="Jump to…"
              aria-label="Jump to"
              className="w-full border-b border-border bg-transparent px-4 py-3 text-sm outline-none"
            />
            <ul className="max-h-72 overflow-auto py-1">
              {hits.length === 0 && (
                <li className="px-4 py-2 text-sm text-dim">no match</li>
              )}
              {hits.map((s, i) => (
                <li key={s.id}>
                  <button
                    type="button"
                    onMouseEnter={() => setSelected(i)}
                    onClick={() => openService(s)}
                    className={`flex w-full items-center gap-2 px-4 py-2 text-left text-sm ${
                      i === safeSelected ? "bg-accent-light text-accent" : ""
                    }`}
                  >
                    <span aria-hidden="true">{s.name[0]?.toUpperCase()}</span>
                    <span>{s.name}</span>
                  </button>
                </li>
              ))}
            </ul>
            <p className="border-t border-border px-4 py-2 text-xs text-dim">
              ↑↓ navigate · Enter open · Esc close
            </p>
          </div>
        </div>
      )}
    </>
  );
}
