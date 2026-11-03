"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { humanSize, relativeTime } from "@/lib/format";
import {
  AuthError,
  fetchIntercom,
  postIntercom,
  postIntercomUpload,
} from "@/lib/api";
import LoginRequired from "@/components/LoginRequired";
import type { IntercomMessage } from "@/lib/types";

/** Delta-poll cadence (ms) — pauses entirely while the tab is hidden. */
const POLL_MS = 7000;
/** Client-side render cap — mirrors the server FIFO cap (INTERCOM_MAX_MESSAGES default). */
const MAX_RENDERED = 50;

/**
 * Team-notes panel (intercom): short notes with optional cloud attachment.
 * Native fetch against the same-origin API (rewritten to the Rust backend).
 * Renders messages as text nodes — note text is user-provided.
 *
 * Live updates via the backend's delta endpoint: poll `?since=<cursor>`
 * every 7s while the tab is visible (visibilitychange triggers an
 * immediate catch-up on show). After sending, the POST response is
 * prepended directly — no extra roundtrip.
 */
export default function IntercomPanel() {
  const [messages, setMessages] = useState<IntercomMessage[]>([]);
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<string | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const latestRef = useRef(0);
  const authRef = useRef(false);
  const pollingRef = useRef(false);

  const markAuthRequired = useCallback(() => {
    authRef.current = true;
    setAuthRequired(true);
  }, []);

  /** Merge incoming notes at the top, dedupe by id, cap the render list. */
  const mergeIncoming = useCallback((incoming: IntercomMessage[]) => {
    if (incoming.length === 0) return;
    setMessages((prev) => {
      const seen = new Set(prev.map((m) => m.id));
      const fresh = incoming.filter((m) => !seen.has(m.id));
      if (fresh.length === 0) return prev;
      return [...fresh, ...prev].slice(0, MAX_RENDERED);
    });
  }, []);

  const fullLoad = useCallback(async () => {
    try {
      const data = await fetchIntercom();
      latestRef.current = data.latest;
      setMessages(data.messages.slice(0, MAX_RENDERED));
    } catch (err) {
      if (err instanceof AuthError) markAuthRequired();
      /* keep last known list */
    }
  }, [markAuthRequired]);

  const pollDelta = useCallback(async () => {
    if (authRef.current || pollingRef.current) return;
    pollingRef.current = true;
    try {
      const since = latestRef.current;
      const data = await fetchIntercom(since > 0 ? since : undefined);
      // Store restarted behind us (reset ids): latest < cursor → resync
      // with a full load instead of an eternally-empty delta.
      if (data.latest < latestRef.current) {
        latestRef.current = 0;
        await fullLoad();
        return;
      }
      latestRef.current = data.latest;
      mergeIncoming(data.messages);
    } catch (err) {
      if (err instanceof AuthError) markAuthRequired();
      /* transient — next tick retries */
    } finally {
      pollingRef.current = false;
    }
  }, [fullLoad, markAuthRequired, mergeIncoming]);

  // Initial load (full list seeds the cursor).
  useEffect(() => {
    void fullLoad();
  }, [fullLoad]);

  // Delta polling: 7s cadence, skipped while hidden, immediate catch-up
  // on visibilitychange.
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) void pollDelta();
    }, POLL_MS);
    const onVisible = () => {
      if (!document.hidden) void pollDelta();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [pollDelta]);

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
      let created: IntercomMessage;
      if (file) {
        created = (await postIntercomUpload(trimmed, file)).message;
      } else {
        created = (
          await postIntercom({
            text: trimmed,
            ...(url.trim() ? { attachment_url: url.trim() } : {}),
          })
        ).message;
      }
      // The POST returns the stored note — prepend it directly instead
      // of triggering another fetch roundtrip.
      latestRef.current = Math.max(latestRef.current, created.id);
      mergeIncoming([created]);
      setText("");
      setUrl("");
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      setOut(null);
    } catch (err) {
      setOut(`Not sent: ${err instanceof Error ? err.message : String(err)}`);
      if (err instanceof AuthError) markAuthRequired();
    } finally {
      setBusy(false);
    }
  }

  if (authRequired) {
    return (
      <section className="tab-panel" aria-label="Notizen">
        <h2 className="mb-3 text-lg font-bold">Notizen</h2>
        <LoginRequired label="Team-Notizen sind angemeldeten Nutzer:innen vorbehalten" />
      </section>
    );
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
            {messages.map((m) => (
              <li key={m.id}>
                {m.text}
                {(m.author || m.created_at) && (
                  <span className="ic-meta text-xs text-dim">
                    {[
                      m.author ? `von ${m.author}` : null,
                      relativeTime(m.created_at ? Number(m.created_at) : null),
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                )}
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
