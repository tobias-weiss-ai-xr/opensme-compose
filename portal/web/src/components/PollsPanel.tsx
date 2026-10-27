"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { humanSize, markVoted, votedSet } from "@/lib/format";
import LoginRequired from "@/components/LoginRequired";
import type { Poll } from "@/lib/types";

/**
 * Team-polls panel (Umfragen — optional card, only shown when the Rust
 * backend has polls enabled: /api/polls returns 404 otherwise).
 */
export default function PollsPanel() {
  const [polls, setPolls] = useState<Poll[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [options, setOptions] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/polls", { cache: "no-store" });
      if (res.status === 401) {
        setAuthRequired(true);
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      setPolls((await res.json()).polls as Poll[]);
    } catch {
      setPolls([]); // feature disabled or unreachable — render placeholder
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Only render anything once we know the feature exists server-side.
  if (polls === null && authRequired) {
    return (
      <section className="tab-panel" aria-label="Umfragen">
        <h2 className="mb-3 text-lg font-bold">Umfragen</h2>
        <LoginRequired label="Umfragen sind angemeldeten Nutzer:innen vorbehalten" />
      </section>
    );
  }

  if (polls === null) {
    return (
      <section className="tab-panel" aria-label="Umfragen">
        <div className="flex h-24 items-center justify-center text-dim">
          Umfragen werden geladen…
        </div>
      </section>
    );
  }

  async function vote(poll: Poll, option: number) {
    try {
      const res = await fetch(`/api/polls/${poll.id}/vote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ option }),
      });
      if (res.status === 401) setAuthRequired(true);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      markVoted(poll.id);
      await refresh();
    } catch (err) {
      setError(`Nicht gewählt: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function create() {
    const q = question.trim();
    const opts = options
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    if (!q || opts.length < 2) {
      setError("Bitte Frage und mindestens 2 Optionen angeben.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const attachmentUrl = url.trim();
      if (file) {
        let reqUrl = `/api/polls/upload?question=${encodeURIComponent(q)}&options=${encodeURIComponent(
          JSON.stringify(opts),
        )}&filename=${encodeURIComponent(file.name)}`;
        if (attachmentUrl) reqUrl += `&attachment_url=${encodeURIComponent(attachmentUrl)}`;
        const res = await fetch(reqUrl, {
          method: "POST",
          headers: { "Content-Type": file.type || "application/octet-stream" },
          body: file,
        });
        if (res.status === 401) setAuthRequired(true);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
      } else {
        const res = await fetch("/api/polls", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question: q,
            options: opts,
            attachment_url: attachmentUrl || "",
          }),
        });
        if (res.status === 401) setAuthRequired(true);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
      }
      setQuestion("");
      setOptions("");
      setUrl("");
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      await refresh();
    } catch (err) {
      setError(`Anlegen fehlgeschlagen: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(false);
    }
  }

  const voted = votedSet();

  return (
    <section className="tab-panel" aria-label="Umfragen">
      <h2 className="mb-3 text-lg font-bold">Umfragen</h2>
      <div className="func-grid single" style={{ gridTemplateColumns: "minmax(220px, 420px)" }}>
        <div className="rounded-[10px] border border-border bg-card p-4 shadow-sm">
          <span className="func-icon" aria-hidden="true">
            📊
          </span>
          <h3 className="font-semibold">Umfragen</h3>
          <p className="mb-2 text-sm text-dim">
            Team-Abstimmungen — optional mit Dokument aus der Cloud
          </p>
          <ul className="pl-list" aria-live="polite">
            {polls.length === 0 && (
              <li className="ic-empty">Noch keine Umfragen — leg die erste an.</li>
            )}
            {polls.map((p) => {
              const total = p.votes.reduce((a, b) => a + b, 0);
              const isVoted = p.closed || voted[p.id];
              return (
                <li key={p.id} className="poll">
                  <div className="poll-q font-medium">{p.question}</div>
                  {p.attachment?.url && (
                    <a
                      className="poll-att"
                      href={p.attachment.url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      📎 {p.attachment.name || "Anhang"}
                    </a>
                  )}
                  <div className="poll-opts">
                    {p.options.map((opt, i) => {
                      const count = p.votes?.[i] || 0;
                      const pct = total ? Math.round((count / total) * 100) : 0;
                      return (
                        <div key={i} className={`poll-opt ${isVoted ? "voted" : ""}`}>
                          <span
                            className="poll-fill"
                            style={{ width: `${pct}%` }}
                            aria-hidden="true"
                          />
                          <span className="poll-label">{opt}</span>
                          <span className="poll-count">
                            {total ? `${count} · ${pct}%` : String(count)}
                          </span>
                          {!isVoted && (
                            <button
                              type="button"
                              className="poll-vote"
                              aria-label={`Für „${opt}“ stimmen`}
                              onClick={() => void vote(p, i)}
                            >
                              Wählen
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </li>
              );
            })}
          </ul>
          <details className="mt-3">
            <summary className="cursor-pointer text-sm text-accent">
              Neue Umfrage anlegen
            </summary>
            <div className="pl-box mt-2">
              <input
                type="text"
                maxLength={300}
                placeholder="Frage…"
                aria-label="Frage"
                className="w-full rounded-lg border border-border p-2 text-sm"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
              />
              <textarea
                rows={3}
                placeholder="Antwortoptionen — eine pro Zeile (min. 2)"
                aria-label="Antwortoptionen"
                className="mt-2 w-full rounded-lg border border-border p-2 text-sm"
                value={options}
                onChange={(e) => setOptions(e.target.value)}
              />
              <input
                type="url"
                placeholder="Anhang aus der Cloud — geteilten Link einfügen (https://cloud…/s/…)"
                aria-label="Umfrage-Anhang-URL"
                className="mt-2 w-full rounded-lg border border-border p-2 text-sm"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
              <div className="mt-2 flex items-center gap-3">
                <label className="cursor-pointer text-sm text-accent" htmlFor="pl-file">
                  📎 Datei…
                </label>
                <input
                  id="pl-file"
                  ref={fileRef}
                  type="file"
                  aria-label="Umfrage-Anhang"
                  hidden
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                />
                <span className="text-xs text-dim" aria-live="polite">
                  {file ? `${file.name} (${humanSize(file.size)})` : ""}
                </span>
                <button
                  className="pl-create ml-auto"
                  type="button"
                  disabled={busy}
                  onClick={() => void create()}
                >
                  Anlegen
                </button>
              </div>
              {error && (
                <p className="mt-2 text-sm text-dim" role="status">
                  {error}
                </p>
              )}
            </div>
          </details>
        </div>
      </div>
    </section>
  );
}
