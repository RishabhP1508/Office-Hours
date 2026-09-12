// Parses bracket citation markers out of rendered answer prose, using EXACTLY the backend's own
// convention (services/orchestrator/app/guardrails/citations.py::parse_cited_indices):
// /\[(\d+(?:\s*,\s*\d+)*)\]/ -- a single index ("[2]") or a comma-separated group ("[1, 3]").
// Phase 4 fixed a prompt-vs-metric mismatch to make this the one true bracket convention; this
// module must never diverge from it by stripping, renumbering, or reformatting a bracket.

const CITATION_SOURCE = String.raw`\[(\d+(?:\s*,\s*\d+)*)\]`;

/** Returns a FRESH RegExp instance matching one citation marker ("[2]" or a comma group like
 * "[1, 3]"), same shape every call. A fresh instance per call, not a shared module-level RegExp,
 * because the `g` flag carries mutable scan position (`lastIndex`) on the object itself, and this
 * function is called from more than one place (this module's own `parseAnswerSegments`, and
 * lib/prose.ts's combined inline tokeniser, which splices `.source` into a larger alternation) --
 * a shared instance would let one caller's scan position clobber another's.
 *
 * Exported so lib/prose.ts can recognize the exact same marker shape inside its single-pass inline
 * tokeniser without hand-copying this pattern into a second regex that could drift from this one. */
export function citationRegex(): RegExp {
  return new RegExp(CITATION_SOURCE, "g");
}

export type AnswerSegment =
  | { kind: "text"; value: string }
  | { kind: "citation"; indices: number[] };

/** Splits `text` into an ordered sequence of plain-text runs and citation-bracket groups. Every
 * character of the original text is preserved in exactly one segment or the other -- nothing is
 * dropped, stripped, or reformatted. */
export function parseAnswerSegments(text: string): AnswerSegment[] {
  const segments: AnswerSegment[] = [];
  let lastIndex = 0;
  const re = citationRegex();
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
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
