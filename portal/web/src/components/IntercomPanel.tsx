"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { humanSize } from "@/lib/format";
import { fetchIntercom, postIntercom, postIntercomUpload } from "@/lib/api";
import type { IntercomMessage } from "@/lib/types";

/**
 * Team-notes panel (intercom): short notes with optional cloud attachment.
 * Native fetch against the same-origin API (rewritten to the Rust backend).
 * Renders messages as text nodes — note text is user-provided.
 */
export default function IntercomPanel() {
  const [messages, setMessages] = useState<IntercomMessage[]>([]);
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await fetchIntercom();
      setMessages(data.messages);
    } catch {
      /* keep last known list */
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function onFileChange(f: File | null) {
    setFile(f);
    if (f) setUrl(""); // either a cloud link or a picked file
  }

  async function send() {
    const trimmed = text.trim();
    if (!trimmed) {
      setOut("Write a short note first.");
      return;
    }
    setBusy(true);
    setOut("…");
    try {
      if (file) {
        await postIntercomUpload(trimmed, file);
      } else {
        await postIntercom({
          text: trimmed,
          ...(url.trim() ? { attachment_url: url.trim() } : {}),
        });
      }
      setText("");
      setUrl("");
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      setOut(null);
      await refresh();
    } catch (err) {
      setOut(`Not sent: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="tab-panel" aria-label="Notizen">
      <h2 className="mb-3 text-lg font-bold">Notizen</h2>
      <div className="func-grid single" style={{ gridTemplateColumns: "minmax(220px, 420px)" }}>
        <div className="bd-1 rounded-[10px] border border-border bg-card p-4 shadow-sm">
          <h3 className="font-semibold">Team-Notizen</h3>
          <p className="mb-2 text-sm text-dim">Kurze Notiz für das Team</p>
          <ul className="ic-list" aria-live="polite">
            {messages.length === 0 && (
              <li className="ic-empty">No notes yet — start the conversation.</li>
            )}
            {messages.map((m, i) => (
              <li key={`${m.text}-${i}`}>
                {m.text}
                {m.attachment && (
                  <a
                    className="ic-att"
                    href={m.attachment.url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    📎 {m.attachment.name}
                    {humanSize(m.attachment.size)
                      ? ` (${humanSize(m.attachment.size)})`
                      : ""}
                    {m.attachment.content_type ? ` · ${m.attachment.content_type}` : ""}
                  </a>
                )}
              </li>
            ))}
          </ul>
          <div className="pt-2">
            <label className="sr-only" htmlFor="ic-text">
              Nachricht
            </label>
            <textarea
              id="ic-text"
              className="ic-text w-full rounded-lg border border-border p-2 text-sm"
              rows={2}
              maxLength={2000}
              placeholder="Kurze Notiz für das Team…"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
            />
            <input
              type="url"
              className="ic-url mt-2 w-full rounded-lg border border-border p-2 text-sm"
              placeholder="Anhang aus der Cloud — geteilten Link einfügen (https://cloud…/s/…)"
              aria-label="Anhang-URL"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
            <div className="mt-2 flex items-center gap-3">
              <label className="cursor-pointer text-sm text-accent" htmlFor="ic-file">
                📎 Datei…
              </label>
              <input
                id="ic-file"
                ref={fileRef}
                type="file"
                hidden
                aria-label="Datei anhängen"
                onChange={(e) => onFileChange(e.target.files?.[0] ?? null)}
              />
              <span className="text-xs text-dim" aria-live="polite">
                {file ? `${file.name} (${humanSize(file.size)})` : ""}
              </span>
              <button
                className="ic-send ml-auto"
                type="button"
                disabled={busy}
                onClick={() => void send()}
              >
                Senden
              </button>
            </div>
            {out && (
              <p className="mt-2 text-sm text-dim" role="status">
                {out}
              </p>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
