/**
 * Sanitize the decoded [[...path]] segments before they are re-joined into
 * the backend URL.
 *
 * Why: Next decodes percent-encoding while resolving catch-all params, so a
 * request like /api/..%2f..%2fmetrics arrives here as ["..", "..", "metrics"].
 * Re-joining and fetching `http://backend/api/../../metrics` normalizes to the
 * backend ROOT path (/metrics) — silently bypassing web gates that only guard
 * routes in front of the proxy (the round-4 /metrics session gate was bypassed
 * this way, exposing all metrics anonymously).
 *
 * Returns a cleaned segment list, or null when the path must be rejected
 * (404 upstream — we don't want to reveal what backend routes exist).
 */
export function sanitizeApiPath(path: string[] | undefined): string[] | null {
  if (!path || path.length === 0) return [];
  for (const seg of path) {
    // "." / ".." would normalize outside /api/*; empty segments (from
    // collapsed slashes) are harmless.
    if (seg === "." || seg === "..") return null;
    // A decoded slash inside a segment (e.g. %2f smuggling: "..%2f..%2fmetrics"
    // arrives as ONE segment "../../metrics") also normalizes out of /api/.
    if (seg.includes("/")) return null;
    // A literal "%" means double-encoded input (e.g. %252e%252e) that would
    // survive our checks and be decoded by the server side later.
    if (seg.includes("%")) return null;
  }
  return path;
}
