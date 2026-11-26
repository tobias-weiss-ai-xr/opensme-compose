"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { humanSize, relativeTime } from "@/lib/format";
import {
  AuthError,
  fetchIntercom,
  postBroadcast,
  postCloudShare,
  postIntercom,
  postIntercomUpload,
  uploadCloudFile,
} from "@/lib/api";
import LoginRequired from "@/components/LoginRequired";
import { aiEnabledProbe, draftReply, summarizeThread } from "@/lib/ai";
import { subscribeEvents, SSE_READY_KIND } from "@/lib/events";
import type { IntercomMessage } from "@/lib/types";
import { useSlot, type CloudPickResult, type SharePayload } from "@/lib/bridge";

/** Delta-poll cadence (ms) — pauses entirely while the tab is hidden. */
const POLL_MS = 7000;
/** Client-side render cap — mirrors the server FIFO cap (INTERCOM_MAX_MESSAGES default). */
const MAX_RENDERED = 50;

interface IntercomPanelProps {
  /** Operator broadcast box (INTERCOM_BROADCAST=1) — off by default. */
  broadcastEnabled?: boolean;
  /**
   * Share-to-team prefill (portal-intercom): a source component (feed,
   * cloud file) hands the composer a reference + link; the shell switches
   * into the notes tab and passes it here. Re-prefills on every NEW object
   * (latest share wins); never clears the user's own typing.
   */
  sharePrefill?: SharePayload | null;
}

/**
 * Team-notes panel (intercom): short notes with optional cloud attachment.
 * Native fetch against the same-origin API (rewritten to the Rust backend).
 * Renders messages as text nodes — note text is user-provided.
 *
 * Live updates via the backend's delta endpoint: poll `?since=<cursor>`
 * every 7s while the tab is visible (visibilitychange triggers an
 * immediate catch-up on show). After sending, the POST response is
 * prepended directly — no extra roundtrip.
 *
 * With broadcastEnabled the panel shows a compact operator broadcast box
 * in the header area: /api/broadcast stores the note AND fans it out to
 * the Element support room and Stalwart mail (all demo users).
 */
export default function IntercomPanel({
  broadcastEnabled = false,
  sharePrefill = null,
}: IntercomPanelProps) {
  const [messages, setMessages] = useState<IntercomMessage[]>([]);
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<string | null>(null);
  const [broadcastText, setBroadcastText] = useState("");
  const [broadcastBusy, setBroadcastBusy] = useState(false);
  const [broadcastOut, setBroadcastOut] = useState<string | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const [cloudPick, setCloudPick] = useState<CloudPickResult | null>(null);
  const [aiReady, setAiReady] = useState(false);
  const [aiBusy, setAiBusy] = useState<null | "summary" | "draft">(null);
  const [aiOut, setAiOut] = useState<string | null>(null);
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

  // Live SSE events (paused poller path): apply deltas directly so notes
  // appear without waiting for the next 7s poll tick.
  useEffect(() => subscribeEvents((event) => {
    if (event.kind === "intercom.created" && event.payload.message) {
      mergeIncoming([event.payload.message as IntercomMessage]);
      latestRef.current = Math.max(latestRef.current, (event.payload.message as IntercomMessage).id);
    } else if (event.kind === "intercom.deleted" && typeof event.payload.id === "number") {
      const id = event.payload.id;
      setMessages((prev) => prev.filter((m) => m.id !== id));
    }
  }), [mergeIncoming]);

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

  // SSE just became live (first connect or reconnect): reconcile with the
  // authoritative store. A fail-soft delete XADD can be lost while the store
  // delete succeeds; replaying an older cursor would otherwise re-add it.
  useEffect(() => subscribeEvents((event) => {
    if (event.kind === SSE_READY_KIND) void fullLoad();
  }), [fullLoad]);

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

  // Delta polling: 7s cadence, skipped while hidden or while the shell's
  // SSE stream is connected, immediate catch-up on visibilitychange.
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden && document.querySelector("main[data-events-connected='1']") === null) void pollDelta();
    }, POLL_MS);
    const onVisible = () => {
      if (!document.hidden && document.querySelector("main[data-events-connected='1']") === null) void pollDelta();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [pollDelta]);

  // AI opt-in probe (OLLAMA_URL gate): one GET /api/ai/summarize on mount.
  // Any failure resolves false — buttons stay hidden, no error spam.
  useEffect(() => {
    let alive = true;
    aiEnabledProbe()
      .then((ok) => {
        if (alive) setAiReady(ok);
      })
      .catch(() => {
        /* probe contract: never rejects — belt-and-braces against mocks */
      });
    return () => {
      alive = false;
    };
  }, []);

  function onFileChange(f: File | null) {
    setFile(f);
    if (f) {
      setUrl(""); // either a cloud link or a picked file
      setCloudPick(null);
    }
  }

  /**
   * Cloud pick: the shared cloud.pick slot (portal-intercom) hands us the
   * selected entry; the backend turns it into a public read-only share and
   * we ride the existing attachment_url plumbing (the Rust attachment
   * validator rejects relative paths but allowlists these share URLs).
   */
  const cloudPicker = useSlot("cloud.pick");
  // portal-tasks: turn a team note into an "Aufgabe" (title = note text,
  // attachment link = source_url). No host → no affordance.
  const taskProvider = useSlot("task.create");
  /** One-line note excerpt usable as a task title. */
  const taskTitle = (text: string) =>
    text.length > 80 ? `${text.slice(0, 79)}…` : text.trim();
  async function onCloudPick(picked: CloudPickResult) {
    setFile(null);
    if (fileRef.current) fileRef.current.value = "";
    setOut("…");
    try {
      const shared = await postCloudShare(picked.path);
      setUrl(shared.url);
      setCloudPick(picked);
      setOut(null);
    } catch (err) {
      setOut(
        `Cloud-Freigabe fehlgeschlagen: ${err instanceof Error ? err.message : String(err)}`,
      );
      if (err instanceof AuthError) markAuthRequired();
    }
  }

  async function pickFromCloud() {
    try {
      const picked = await cloudPicker?.pick();
      if (picked) await onCloudPick(picked);
    } catch (err) {
      setOut(
        `Cloud-Freigabe fehlgeschlagen: ${err instanceof Error ? err.message : String(err)}`,
      );
      if (err instanceof AuthError) markAuthRequired();
    }
  }

  /**
   * "In Cloud ablegen" (portal-intercom): stores the locally picked file
   * in the cloud on explicit demand and switches the note to the durable
   * share — the ephemeral intercom upload is never used in this path.
   */
  async function storeInCloud(f: File) {
    try {
      const { path } = await uploadCloudFile(f.name, f, f.type);
      const shared = await postCloudShare(path);
      setUrl(shared.url);
      setCloudPick({ path, name: f.name, size: f.size });
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      setOut("In der Cloud abgelegt — Anhang wird als geteilter Cloud-Link gesendet.");
    } catch (err) {
      setOut(
        `Cloud-Freigabe fehlgeschlagen: ${err instanceof Error ? err.message : String(err)}`,
      );
      if (err instanceof AuthError) markAuthRequired();
    }
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
      setCloudPick(null);
      if (fileRef.current) fileRef.current.value = "";
      setOut(null);
    } catch (err) {
      setOut(`Not sent: ${err instanceof Error ? err.message : String(err)}`);
      if (err instanceof AuthError) markAuthRequired();
    } finally {
      setBusy(false);
    }
  }

  /**
   * Operator broadcast: store + fan out via /api/broadcast (server-side).
   * Per-channel status in German — "Broadcast gesendet — Element ✓ · Mail ✓",
   * failures named inline; the note itself is stored regardless.
   */
  async function broadcast() {
    const trimmed = broadcastText.trim();
    if (!trimmed) {
      setBroadcastOut("Erst einen kurzen Broadcast-Text schreiben.");
      return;
    }
    setBroadcastBusy(true);
    setBroadcastOut("…");
    try {
      const res = await postBroadcast(trimmed);
      latestRef.current = Math.max(latestRef.current, res.message.id);
      mergeIncoming([res.message]);
      setBroadcastText("");
      setBroadcastOut(
        res.queued
          ? "Broadcast gespeichert und zur Zustellung vorgemerkt."
          : "Broadcast gespeichert, Zustellung nicht vorgemerkt.",
      );
    } catch (err) {
      setBroadcastOut(`Nicht gesendet: ${err instanceof Error ? err.message : String(err)}`);
      if (err instanceof AuthError) markAuthRequired();
    } finally {
      setBroadcastBusy(false);
    }
  }

  /**
   * AI helper (T6): summarize the thread / suggest a reply to the newest
   * note via the mocked-LLM backend routes. German status line like the
   * broadcast box; 401 falls back to the existing login CTA.
   */
  async function runAi(kind: "summary" | "draft") {
    const target = messages[0]?.id; // newest note anchors the thread
    if (!target) {
      setAiOut("Keine Notizen vorhanden — erst etwas notieren.");
      return;
    }
    setAiBusy(kind);
    setAiOut("…");
    try {
      setAiOut(
        kind === "summary"
          ? (await summarizeThread(target)).summary
          : (await draftReply(target)).draft,
      );
    } catch (err) {
      setAiOut(`KI-Fehler: ${err instanceof Error ? err.message : String(err)}`);
      if (err instanceof AuthError) markAuthRequired();
    } finally {
      setAiBusy(null);
    }
  }

  // Share-to-team prefill: apply every NEW prefill object once (identity
  // guard), fill title+link into the text and a cloud share into the URL
  // field, then confirm with an inline status — the composer stays fully
  // editable. No prefill → zero behaviour change (fail-soft).
  const lastAppliedRef = useRef<SharePayload | null>(null);
  useEffect(() => {
    if (!sharePrefill || sharePrefill === lastAppliedRef.current) return;
    lastAppliedRef.current = sharePrefill;
    const link = sharePrefill.url ?? "";
    setText(link ? `${sharePrefill.text}\n${link}` : sharePrefill.text);
    if (sharePrefill.attachmentUrl) setUrl(sharePrefill.attachmentUrl);
    setOut("Aus Quelle übernommen — Text anpassen und senden.");
  }, [sharePrefill]);

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
      {broadcastEnabled && (
        <div className="bc-row mb-3 rounded-[10px] border border-border bg-card p-4 shadow-sm">
          <h3 className="font-semibold">Broadcast an alle</h3>
          <p className="mb-2 text-sm text-dim">
            Operator-Nachricht an Team-Notizen, Element-Raum und Mail-Postfächer
            aller Demo-Nutzer:innen
          </p>
          <div className="flex items-center gap-2">
            <label className="sr-only" htmlFor="bc-text">
              Broadcast an alle
            </label>
            <input
              id="bc-text"
              className="bc-text w-full rounded-lg border border-border p-2 text-sm"
              maxLength={2000}
              placeholder="Broadcast an alle…"
              value={broadcastText}
              onChange={(e) => setBroadcastText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void broadcast();
                }
              }}
            />
            <button
              className="bc-send shrink-0"
              type="button"
              disabled={broadcastBusy}
              onClick={() => void broadcast()}
            >
              Broadcast an alle
            </button>
          </div>
          {broadcastOut && (
            <p className="mt-2 text-sm text-dim" role="status">
              {broadcastOut}
            </p>
          )}
        </div>
      )}
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
                {taskProvider && (
                  <button
                    type="button"
                    className="ic-task mt-1 block text-xs text-accent underline"
                    onClick={() =>
                      taskProvider.create({
                        title: taskTitle(m.text) || "Team-Notiz",
                        url: m.attachment?.url ?? undefined,
                      })
                    }
                  >
                    Als Aufgabe anlegen
                  </button>
                )}
              </li>
            ))}
          </ul>
          {aiReady && (
            <div className="ai-row mt-2">
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  className="ai-summarize text-sm text-accent"
                  disabled={aiBusy !== null}
                  onClick={() => void runAi("summary")}
                >
                  ✨ Zusammenfassen
                </button>
                <button
                  type="button"
                  className="ai-draft text-sm text-accent"
                  disabled={aiBusy !== null}
                  onClick={() => void runAi("draft")}
                >
                  ✏ Antwort vorschlagen
                </button>
              </div>
              {aiOut && (
                <p className="ai-out mt-2 text-sm text-dim" role="status">
                  {aiOut}
                </p>
              )}
            </div>
          )}
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
              {cloudPicker && (
                <button
                  type="button"
                  className="ic-cloud text-sm text-accent"
                  onClick={() => void pickFromCloud()}
                >
                  ☁ Aus der Cloud anhängen
                </button>
              )}
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
                {file
                  ? `${file.name} (${humanSize(file.size)})`
                  : cloudPick
                    ? `☁ ${cloudPick.name}${humanSize(cloudPick.size) ? ` (${humanSize(cloudPick.size)})` : ""}`
                    : ""}
              </span>
              {file && (
                <button
                  type="button"
                  className="ic-cloud-save text-xs text-accent"
                  onClick={() => void storeInCloud(file)}
                >
                  ☁ In Cloud ablegen
                </button>
              )}
              {cloudPick && (
                <button
                  type="button"
                  className="ic-cloud-clear text-xs text-dim"
                  aria-label="Cloud-Anhang entfernen"
                  onClick={() => {
                    setCloudPick(null);
                    setUrl("");
                  }}
                >
                  ✕
                </button>
              )}
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
