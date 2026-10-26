"use client";

import { useState } from "react";
import { chatWithAi } from "@/lib/api";

/**
 * AI assistant card — only server-rendered when the operator configured
 * AI_API_URL (the Rust backend mounts /api/ai/chat only then).
 */
export default function AiCard() {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function ask() {
    const q = question.trim();
    if (!q) return;
    setBusy(true);
    setAnswer("…");
    try {
      const d = await chatWithAi({ question: q });
      setAnswer(d.answer || "(empty answer)");
    } catch (err) {
      setAnswer(`AI unavailable: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card func-card ai" id="ai-card">
      <span className="func-icon" aria-hidden="true">
        🧠
      </span>
      <h3>KI-Assistent</h3>
      <p className="product">Fragen an deine Wissensbasis</p>
      <div className="ai-box mt-2">
        <input
          className="ai-q w-full rounded-lg border border-border p-2 text-sm"
          type="text"
          placeholder="Frage stellen…"
          aria-label="Frage"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void ask();
            }
          }}
        />
        <button className="ai-ask mt-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-accent-hover" type="button" disabled={busy} onClick={() => void ask()}>
          {busy ? "…" : "Stellen"}
        </button>
        {answer !== null && (
          <p className="ai-out mt-2 text-sm text-dim" role="status">
            {answer}
          </p>
        )}
      </div>
    </div>
  );
}
