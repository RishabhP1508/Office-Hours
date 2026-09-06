// Parses bracket citation markers out of rendered answer prose, using EXACTLY the backend's own
// convention (services/orchestrator/app/guardrails/citations.py::parse_cited_indices):
// /\[(\d+(?:\s*,\s*\d+)*)\]/ -- a single index ("[2]") or a comma-separated group ("[1, 3]").
// Phase 4 fixed a prompt-vs-metric mismatch to make this the one true bracket convention; this
// module must never diverge from it by stripping, renumbering, or reformatting a bracket.

const CITATION_RE = /\[(\d+(?:\s*,\s*\d+)*)\]/g;

export type AnswerSegment =
  | { kind: "text"; value: string }
  | { kind: "citation"; indices: number[] };

/** Splits `text` into an ordered sequence of plain-text runs and citation-bracket groups. Every
 * character of the original text is preserved in exactly one segment or the other -- nothing is
 * dropped, stripped, or reformatted. */
export function parseAnswerSegments(text: string): AnswerSegment[] {
  const segments: AnswerSegment[] = [];
  let lastIndex = 0;
  CITATION_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = CITATION_RE.exec(text)) !== null) {
    if (match.index > lastIndex) {
      segments.push({ kind: "text", value: text.slice(lastIndex, match.index) });
    }
    const indices = match[1].split(",").map((part) => Number.parseInt(part.trim(), 10));
    segments.push({ kind: "citation", indices });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ kind: "text", value: text.slice(lastIndex) });
  }
  return segments;
}
