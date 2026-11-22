"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AuthError,
  createTask,
  deleteTask,
  fetchTasks,
  fetchTaskUsers,
  patchTask,
} from "@/lib/api";
import LoginRequired from "@/components/LoginRequired";
import { subscribeEvents } from "@/lib/events";
import type { TeamTask } from "@/lib/types";
import type { TaskCreatePayload } from "@/lib/bridge";

/** Delta-poll cadence (ms) — pauses entirely while the tab is hidden. */
const POLL_MS = 7000;

const FILTERS = [
  { id: "all", label: "Alle" },
  { id: "mine", label: "Meine" },
  { id: "open", label: "Offen" },
  { id: "done", label: "Erledigt" },
] as const;

type FilterId = (typeof FILTERS)[number]["id"];

interface TasksPanelProps {
  /**
   * task.create prefill (portal-tasks bridge): a source component (feed
   * card, note, cloud file row) hands a reference; the shell switches into
   * the Aufgaben tab and passes it here. Re-prefills on every NEW object;
   * never clears the user's own typing afterwards.
   */
  taskPrefill?: TaskCreatePayload | null;
}

function todayIso(): string {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

/** Open first, then due date asc (no due last), priority high→low, newest first. */
function sortTasks(tasks: TeamTask[]): TeamTask[] {
  const prio: Record<string, number> = { high: 0, normal: 1, low: 2 };
  return [...tasks].sort((a, b) => {
    if (a.status !== b.status) return a.status === "open" ? -1 : 1;
    const da = a.due ?? "9999-12-31";
    const db = b.due ?? "9999-12-31";
    if (da !== db) return da < db ? -1 : 1;
    const p = (prio[a.priority] ?? 1) - (prio[b.priority] ?? 1);
    if (p !== 0) return p;
    return b.id - a.id;
  });
}

function isOverdue(t: TeamTask): boolean {
  return t.status === "open" && !!t.due && t.due < todayIso();
}

const PRIO_LABEL: Record<string, string> = {
  high: "hoch",
  normal: "normal",
  low: "niedrig",
};

/**
 * Team-tasks panel ("Aufgaben" tab, portal-tasks): create, filter, sort,
 * complete and delete tasks. Author is stamped server-side from the
 * session; only assignee or author may complete/delete (server answers
 * 403 — shown inline, the form is preserved).
 *
 * Live updates via the backend's delta endpoint: poll `?since=<cursor>`
 * every 7s while visible (visibilitychange triggers an immediate catch-up).
 */
export default function TasksPanel({ taskPrefill = null }: TasksPanelProps) {
  const [tasks, setTasks] = useState<TeamTask[]>([]);
  const [filter, setFilter] = useState<FilterId>("all");
  const [users, setUsers] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [assignee, setAssignee] = useState("");
  const [due, setDue] = useState("");
  const [priority, setPriority] = useState("normal");
  const [sourceUrl, setSourceUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const latestRef = useRef(0);
  const authRef = useRef(false);
  const pollingRef = useRef(false);
  const filterRef = useRef<FilterId>("all");
  filterRef.current = filter;

  const markAuthRequired = useCallback(() => {
    authRef.current = true;
    setAuthRequired(true);
  }, []);

  /** Merge incoming tasks (dedupe by id), keep the sort order. */
  const mergeIncoming = useCallback((incoming: TeamTask[]) => {
    if (incoming.length === 0) return;
    setTasks((prev) => {
      const seen = new Set(prev.map((t) => t.id));
      const fresh = incoming.filter((t) => !seen.has(t.id));
      if (fresh.length === 0) return prev;
      return sortTasks([...fresh, ...prev]);
    });
  }, []);

  // Live SSE events (paused poller path): apply deltas directly, keeping the
  // same sorted list as the poll/full-load paths. Delete payloads carry the
  // full task too, so all three kinds can key off payload.task.
  useEffect(() => subscribeEvents((event) => {
    if (!event.kind.startsWith("task.") || !event.payload.task) return;
    const incoming = event.payload.task as TeamTask;
    setTasks((prev) => {
      if (event.kind === "task.deleted") return prev.filter((t) => t.id !== incoming.id);
      const others = prev.filter((t) => t.id !== incoming.id);
      return sortTasks([incoming, ...others]);
    });
  }), [setTasks]);

  const fullLoad = useCallback(
    async (f: FilterId = filterRef.current) => {
      try {
        const data = await fetchTasks(f === "all" ? undefined : f);
        latestRef.current = data.latest;
        setTasks(sortTasks(data.tasks ?? []));
      } catch (err) {
        if (err instanceof AuthError) markAuthRequired();
        /* keep last known list */
      }
    },
    [markAuthRequired],
  );

  const pollDelta = useCallback(async () => {
    if (authRef.current || pollingRef.current) return;
    pollingRef.current = true;
    try {
      const since = latestRef.current;
      const data = await fetchTasks(
        filterRef.current === "all" ? undefined : filterRef.current,
        since > 0 ? since : undefined,
      );
      // Store restarted behind us (reset ids): latest < cursor → resync.
      if (data.latest < latestRef.current) {
        latestRef.current = 0;
        await fullLoad();
        return;
      }
      latestRef.current = data.latest;
      mergeIncoming(data.tasks);
    } catch (err) {
      if (err instanceof AuthError) markAuthRequired();
      /* transient — next tick retries */
    } finally {
      pollingRef.current = false;
    }
  }, [fullLoad, markAuthRequired, mergeIncoming]);

  // Initial load + assignee suggestions (both once).
  useEffect(() => {
    void fullLoad("all");
    fetchTaskUsers()
      .then((d) => setUsers(d.users ?? []))
      .catch(() => {
        /* suggestions are best-effort — free text always allowed */
      });
  }, [fullLoad]);

  // Delta polling: 7s cadence, skipped while hidden or while the shell's
  // SSE stream is connected, catch-up on show.
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

  // task.create prefill from the shell (feed/note/cloud source).
  useEffect(() => {
    if (!taskPrefill) return;
    setTitle(taskPrefill.title);
    setSourceUrl(taskPrefill.url ?? "");
    setError(null);
  }, [taskPrefill]);

  function onFilterChange(f: FilterId) {
    setFilter(f);
    setError(null);
    void fullLoad(f);
  }

  async function send() {
    if (!title.trim() || !assignee.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createTask({
        title: title.trim(),
        description: description.trim() || undefined,
        assignee: assignee.trim(),
        priority,
        due: due || undefined,
        source_url: sourceUrl.trim() || undefined,
      });
      setTasks((prev) => sortTasks([created, ...prev]));
      latestRef.current = Math.max(latestRef.current, created.id);
      setTitle("");
      setDescription("");
      setDue("");
      setPriority("normal");
      setSourceUrl("");
    } catch (err) {
      // 403/400 etc. surface inline — the form keeps the user's input.
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function toggleDone(t: TeamTask) {
    setError(null);
    try {
      const updated = await patchTask(t.id, {
        status: t.status === "done" ? "open" : "done",
      });
      setTasks((prev) =>
        sortTasks(prev.map((x) => (x.id === t.id ? updated : x))),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function remove(t: TeamTask) {
    setError(null);
    try {
      await deleteTask(t.id);
      setTasks((prev) => prev.filter((x) => x.id !== t.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  if (authRequired) {
    return (
      <section className="tab-panel" aria-label="Aufgaben">
        <h2 className="mb-3 text-lg font-bold">Aufgaben</h2>
        <LoginRequired label="Aufgaben" />
      </section>
    );
  }

  return (
    <section className="tab-panel" aria-label="Aufgaben">
      <h2 className="mb-3 text-lg font-bold">Aufgaben</h2>

      <div className="ts-box rounded-[10px] border border-border bg-card p-4 shadow-sm">
        <h3 className="font-semibold">Neue Aufgabe</h3>
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
          <div>
            <label className="sr-only" htmlFor="ts-title">
              Titel
            </label>
            <input
              id="ts-title"
              type="text"
              className="w-full rounded-lg border border-border p-2 text-sm"
              maxLength={200}
              placeholder="Titel der Aufgabe"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>
          <div>
            <label className="sr-only" htmlFor="ts-assignee">
              Zuständig
            </label>
            <input
              id="ts-assignee"
              type="text"
              className="w-full rounded-lg border border-border p-2 text-sm"
              list="ts-users"
              maxLength={100}
              placeholder="Zuständig (Name oder E-Mail)"
              value={assignee}
              onChange={(e) => setAssignee(e.target.value)}
            />
            <datalist id="ts-users">
              {users.map((u) => (
                <option key={u} value={u} />
              ))}
            </datalist>
          </div>
          <div>
            <label className="sr-only" htmlFor="ts-due">
              Fällig am
            </label>
            <input
              id="ts-due"
              type="date"
              className="w-full rounded-lg border border-border p-2 text-sm"
              value={due}
              onChange={(e) => setDue(e.target.value)}
            />
          </div>
          <div>
            <label className="sr-only" htmlFor="ts-priority">
              Priorität
            </label>
            <select
              id="ts-priority"
              className="w-full rounded-lg border border-border p-2 text-sm"
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            >
              <option value="normal">Priorität: normal</option>
              <option value="high">Priorität: hoch</option>
              <option value="low">Priorität: niedrig</option>
            </select>
          </div>
          <div className="sm:col-span-2">
            <label className="sr-only" htmlFor="ts-desc">
              Beschreibung
            </label>
            <textarea
              id="ts-desc"
              className="w-full rounded-lg border border-border p-2 text-sm"
              rows={2}
              maxLength={2000}
              placeholder="Beschreibung (optional)"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          {sourceUrl && (
            <div className="sm:col-span-2">
              <label className="sr-only" htmlFor="ts-url">
                Quellen-Link
              </label>
              <input
                id="ts-url"
                type="url"
                className="w-full rounded-lg border border-border p-2 text-sm"
                placeholder="Quellen-Link (https://…)"
                value={sourceUrl}
                onChange={(e) => setSourceUrl(e.target.value)}
              />
            </div>
          )}
        </div>
        <button
          type="button"
          className="ts-send mt-2"
          disabled={busy || !title.trim() || !assignee.trim()}
          onClick={() => void send()}
        >
          {busy ? "Speichern…" : "Aufgabe anlegen"}
        </button>
        {error && (
          <p className="ts-error mt-2 text-sm text-red-600" role="alert">
            {error}
          </p>
        )}
      </div>

      <div className="ts-filters mt-4 flex flex-wrap items-center gap-2" role="group" aria-label="Filter">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            className={`ts-filter ${filter === f.id ? "ts-filter-active" : ""}`}
            aria-pressed={filter === f.id}
            onClick={() => onFilterChange(f.id)}
          >
            {f.label}
          </button>
        ))}
      </div>

      <ul className="ts-list mt-2" aria-live="polite">
        {tasks.length === 0 && (
          <li className="ic-empty">Noch keine Aufgaben.</li>
        )}
        {tasks.map((t) => (
          <li key={t.id} className={`ts-item ${t.status === "done" ? "ts-done" : ""}`}>
            <label className="flex items-start gap-2">
              <input
                type="checkbox"
                className="ts-toggle mt-1"
                aria-label={`„${t.title}“ als ${t.status === "done" ? "offen" : "erledigt"} markieren`}
                checked={t.status === "done"}
                onChange={() => void toggleDone(t)}
              />
              <span className="min-w-0 flex-1">
                <span className="ts-title font-medium">{t.title}</span>
                <span className="ts-meta mt-0.5 block text-xs text-dim">
                  {t.assignee} · {PRIO_LABEL[t.priority] ?? t.priority}
                  {t.due && (
                    <span className={isOverdue(t) ? "ts-overdue" : ""}>
                      {" "}· fällig {t.due}
                      {isOverdue(t) ? " (überfällig)" : ""}
                    </span>
                  )}
                  {t.author && <span> · von {t.author}</span>}
                </span>
                {t.description && (
                  <span className="ts-desc mt-0.5 block text-sm">{t.description}</span>
                )}
                {t.source_url && (
                  <a
                    className="ic-att"
                    href={t.source_url}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    Quelle öffnen
                  </a>
                )}
              </span>
              <button
                type="button"
                className="ts-del text-xs text-dim"
                aria-label={`„${t.title}“ löschen`}
                onClick={() => void remove(t)}
              >
                ✕
              </button>
            </label>
          </li>
        ))}
      </ul>
    </section>
  );
}
