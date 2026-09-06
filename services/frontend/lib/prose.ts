// Turns the generator's plain-paragraph answer text (app/prompts.py rule 6 forbids headings and
// heavy bold, so paragraphs on blank lines are the only structure there is) into the pieces
// Message.tsx renders: an optional large serif lead sentence, the remaining paragraphs, with the
// deterministic freshness-notice trailing paragraph removed when present.

// This exact sentence shape is appended by app/guardrails/freshness.py::freshness_notice_text via
// app/pipeline.py -- never written by the model. Anchoring on it is stable because it comes from
// our own code, not the model's. The EffectiveDateNotice component renders the same information
// from the structured `freshness.notices` array instead, so dropping this one paragraph avoids
// saying it twice. If a paragraph does NOT match this pattern, it is left completely alone --
// model-written text is never removed.
const FRESHNESS_NOTICE_PARAGRAPH_RE =
  /^(One|Some) of the sources above describes? a rule that (takes|took) effect on /;

/** Splits on blank lines, the only paragraph boundary the generator ever produces. */
export function splitParagraphs(answer: string): string[] {
  return answer
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter((paragraph) => paragraph.length > 0);
}

export function withoutDuplicateFreshnessNoticeParagraph(paragraphs: string[]): string[] {
  if (paragraphs.length === 0) return paragraphs;
  const last = paragraphs[paragraphs.length - 1];
  if (FRESHNESS_NOTICE_PARAGRAPH_RE.test(last)) {
    return paragraphs.slice(0, -1);
  }
  return paragraphs;
}

// A sentence boundary: '.', '!', or '?' followed by whitespace or the end of the string. Not
// perfect sentence segmentation (an abbreviation like "U.S." can still trip it), but good enough
// to find "the first sentence" of a paragraph without a full NLP dependency, which CLAUDE.md's
// "build least" instruction for this phase rules out anyway.
const SENTENCE_BOUNDARY_RE = /[.!?](?=\s|$)/;

export interface LeadSplit {
  lead: string | null;
  paragraphs: string[];
}

/** Promotes the first sentence of the first paragraph to a standalone lead ONLY if it is <=90
 * characters and more content follows it (either later in the same paragraph, or in a later
 * paragraph) -- otherwise the first paragraph renders exactly as any other paragraph does. */
export function deriveLead(paragraphs: string[]): LeadSplit {
  if (paragraphs.length === 0) {
    return { lead: null, paragraphs };
  }
  const [first, ...rest] = paragraphs;
  const boundary = first.search(SENTENCE_BOUNDARY_RE);
  if (boundary === -1) {
    return { lead: null, paragraphs };
  }
  const candidate = first.slice(0, boundary + 1);
  const remainderOfFirstParagraph = first.slice(boundary + 1).trim();
  const moreContentFollows = remainderOfFirstParagraph.length > 0 || rest.length > 0;
  if (candidate.length > 90 || !moreContentFollows) {
    return { lead: null, paragraphs };
  }
  const remainingParagraphs = remainderOfFirstParagraph.length > 0 ? [remainderOfFirstParagraph, ...rest] : rest;
  return { lead: candidate, paragraphs: remainingParagraphs };
}
