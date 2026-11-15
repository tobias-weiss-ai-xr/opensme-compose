"use client";

import { useCallback, useEffect, useState } from "react";
import { humanSize } from "@/lib/format";

/** One entry of the /api/cloud/list contract (files only). */
export interface CloudFileEntry {
  name: string;
  size: number | null;
  mime: string;
  path: string;
}

interface CloudFilePickerProps {
  open: boolean;
  onClose: () => void;
  /** Receives the picked file — parents typically turn it into a share link. */
  onSelect: (file: CloudFileEntry) => void;
}

function fileEmoji(mime: string): string {
  if (mime.startsWith("image/")) return "🖼️";
  if (mime.startsWith("video/")) return "🎬";
  if (mime.startsWith("audio/")) return "🎵";
  if (mime === "application/zip") return "🗜️";
  return "📄";
}

/**
 * Modal file picker for the cloud (WebDAV) — flat listing of one
 * directory via GET /api/cloud/list. Picking hands the raw entry to
 * onSelect; share-link creation stays the parent's job. German UI.
 *
 * Every row carries a 'KI-Analyse' action (T2): POST /api/ai/docs
 * summarizes/answers questions about the file, shown in an inline
 * result panel below the row (loading / error / answer states).
 */
export default function CloudFilePicker({
  open,
  onClose,
  onSelect,
}: CloudFilePickerProps) {
  const [files, setFiles] = useState<CloudFileEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // KI-Analyse state — one inline panel at a time, keyed by file path.
  const [aiPath, setAiPath] = useState<string | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const [aiAnswer, setAiAnswer] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/cloud/list", { cache: "no-store" });
      if (!res.ok) {
        let message = `HTTP ${res.status}`;
        try {
          const data = await res.json();
          if (data?.error) message = data.error;
        } catch {
          /* keep HTTP fallback */
        }
        throw new Error(message);
      }
      const data = (await res.json()) as CloudFileEntry[];
      setFiles(Array.isArray(data) ? data : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const runAi = useCallback(async (file: CloudFileEntry) => {
    setAiPath(file.path);
    setAiLoading(true);
    setAiError(null);
    setAiAnswer(null);
    try {
      const res = await fetch("/api/ai/docs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: file.path }),
        cache: "no-store",
      });
      if (!res.ok) {
        let message = `HTTP ${res.status}`;
        try {
          const data = await res.json();
          if (data?.error) message = data.error;
        } catch {
          /* keep HTTP fallback */
        }
        throw new Error(message);
      }
      const data = (await res.json()) as { answer?: unknown };
      setAiAnswer(typeof data?.answer === "string" ? data.answer : "");
    } catch (err) {
      setAiError(err instanceof Error ? err.message : String(err));
    } finally {
      setAiLoading(false);
    }
  }, []);

  // (Re)load on every open — the cloud listing is never cached client-side.
  // Closing resets the KI-Analyse panel so a stale answer never reappears.
  useEffect(() => {
    if (open) {
      void load();
    } else {
      setAiPath(null);
      setAiLoading(false);
      setAiError(null);
      setAiAnswer(null);
    }
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const showList = !loading && !error && files !== null && files.length > 0;
  const showEmpty = !loading && !error && files !== null && files.length === 0;

  return (
    <div
      className="cfp-overlay fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="cfp-title"
        className="cfp w-[min(560px,92vw)] rounded-[10px] border border-border bg-card p-4 shadow-sm"
      >
        <div className="mb-1 flex items-center">
          <h3 id="cfp-title" className="font-semibold">
            Aus der Cloud anhängen
          </h3>
          <button
            type="button"
            className="cfp-close ml-auto text-dim"
            aria-label="Schließen"
            onClick={onClose}
          >
            ✕
          </button>
        </div>
        <p className="mb-3 text-sm text-dim">
          Datei wählen — sie wird als geteilter Cloud-Link angehängt.
        </p>

        {loading && <p className="cfp-loading text-sm text-dim">Lade Dateien…</p>}

        {error && (
          <div className="cfp-error">
            <p className="text-sm" role="alert">
              Cloud-Liste fehlgeschlagen ({error})
            </p>
            <button
              type="button"
              className="cfp-retry mt-2 rounded-lg border border-border px-3 py-1 text-sm text-accent"
              onClick={() => void load()}
            >
              Erneut versuchen
            </button>
          </div>
        )}

        {showEmpty && (
          <p className="cfp-empty text-sm text-dim">Keine Dateien in der Cloud</p>
        )}

        {showList && (
          <ul className="cfp-list max-h-[50vh] list-none overflow-y-auto">
            {files!.map((f) => (
              <li key={f.path} className="mb-1">
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="cfp-item min-w-0 flex-1 rounded-lg border border-border px-3 py-2 text-left text-sm hover:bg-accent/10"
                    onClick={() => {
                      onSelect(f);
                      onClose();
                    }}
                  >
                    <span aria-hidden="true">{fileEmoji(f.mime)}</span> {f.name}
                    {humanSize(f.size) ? (
                      <span className="text-dim"> ({humanSize(f.size)})</span>
                    ) : null}
                  </button>
                  <button
                    type="button"
                    className="cfp-ai shrink-0 rounded-lg border border-border px-2 py-2 text-xs text-accent hover:bg-accent/10"
                    onClick={() => void runAi(f)}
                  >
                    KI-Analyse
                  </button>
                </div>
                {aiPath === f.path && (
                  <div className="cfp-ai-panel mt-1 rounded-lg border border-border p-2">
                    <p className="cfp-ai-title mb-1 text-xs font-semibold uppercase tracking-wide text-dim">
                      KI-Analyse
                    </p>
                    {aiLoading && (
                      <p className="cfp-ai-loading text-sm text-dim">
                        Analysiere Datei…
                      </p>
                    )}
                    {aiError && (
                      <p role="alert" className="cfp-ai-error text-sm">
                        Analyse fehlgeschlagen ({aiError})
                      </p>
                    )}
                    {aiAnswer !== null && !aiLoading && !aiError && (
                      <p className="cfp-ai-answer whitespace-pre-wrap text-sm">
                        {aiAnswer}
                      </p>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}

        <div className="mt-3 flex justify-end">
          <button
            type="button"
            className="cfp-cancel rounded-lg border border-border px-3 py-1 text-sm text-dim"
            onClick={onClose}
          >
            Abbrechen
          </button>
        </div>
      </div>
    </div>
  );
}
