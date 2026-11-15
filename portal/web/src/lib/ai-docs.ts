/**
 * Doc-analysis pipeline for POST /api/ai/docs (T2-opencloud-ai) — the
 * runAiHelper-style core behind the route, framework-free and directly
 * unit-testable with a stubbed global fetch (ai-docs.test.ts).
 *
 * Pipeline: OLLAMA_URL gate → {path, question?} validation → WebDAV
 * download via lib/clouddav.ts → text extraction (422 for binaries /
 * files without a text layer) → German instruction → Ollama chat →
 * shaped {status, payload}. Every failure comes back as a plain
 * {status, payload:{error|enabled}} — never an exception.
 */
import {
  aiEnabled,
  LlmError,
  ollamaChat,
  type ChatMessage,
} from "@/lib/llm";
import {
  downloadFile,
  UpstreamError,
  validateRelPath,
  type CloudFileDownload,
} from "@/lib/clouddav";

export interface AiDocsRequest {
  /** Body of the incoming POST (already read). */
  body: { path?: unknown; question?: unknown } | null;
}

export interface AiDocsResult {
  status: number;
  payload: Record<string, unknown>;
}

/** Question budget — keeps the prompt sane for pathological input. */
const QUESTION_MAX = 2_000;

/**
 * Prompt for one doc analysis: question mode answers the user's
 * question with the file as context; otherwise the default German
 * summary instruction (3–5 sentences) applies. Both embed the raw file
 * text so the model works on the document, not on a description of it.
 */
export function docMessages(input: {
  name: string;
  text: string;
  question: string | null;
}): ChatMessage[] {
  const task = input.question
    ? `Beantworte die folgende Frage anhand des Dokuments. Frage: ${input.question}`
    : "Fasse das Dokument in 3-5 deutschen Sätzen zusammen";
  return [
    {
      role: "system",
      content:
        "Du bist ein hilfreicher Assistent, der Dokumente aus der " +
        "firmeneigenen OpenCloud analysiert. Antworte auf Deutsch, sachlich " +
        "und kompakt — NUR mit dem Inhalt, ohne Einleitung, ohne Meta.",
    },
    {
      role: "user",
      content: `${task}.\n\nDokument "${input.name}":\n\n${input.text}`,
    },
  ];
}

/**
 * Shared /api/ai/docs pipeline (the route wraps the result in JSON):
 * gate → validate → download → text check → prompt → LLM → {answer}.
 */
export async function runDocAnalysis(input: AiDocsRequest): Promise<AiDocsResult> {
  if (!aiEnabled()) {
    return { status: 503, payload: { enabled: false } };
  }

  // No leading ../, strip leading / — one shared validator with the
  // cloud listing route (empty path is a file request → invalid here).
  const path = validateRelPath(
    typeof input.body?.path === "string" ? input.body.path : null,
  );
  if (path === null || path === "") {
    return { status: 400, payload: { error: "Pfad fehlt oder ungültig" } };
  }

  const rawQuestion =
    typeof input.body?.question === "string" ? input.body.question.trim() : "";
  const question =
    rawQuestion !== "" ? rawQuestion.slice(0, QUESTION_MAX) : null;

  let file: CloudFileDownload;
  try {
    file = await downloadFile(path);
  } catch (err) {
    if (err instanceof UpstreamError) {
      return {
        status: 502,
        payload: { error: `Cloud-Download fehlgeschlagen (HTTP ${err.status})` },
      };
    }
    const message =
      err instanceof Error && err.message
        ? err.message
        : "Cloud nicht erreichbar";
    return { status: 502, payload: { error: message } };
  }

  // Text extraction yielded nothing (binaries, PDFs without a text
  // layer, empty files) — the LLM has nothing to work with.
  if (!file.text || !file.text.trim()) {
    return {
      status: 422,
      payload: { error: "Dateityp nicht unterstützbar" },
    };
  }

  try {
    const answer = await ollamaChat(
      docMessages({ name: file.name, text: file.text, question }),
    );
    return { status: 200, payload: { answer } };
  } catch (err) {
    const message = err instanceof LlmError ? err.message : "KI nicht erreichbar";
    return { status: 502, payload: { error: message } };
  }
}
