// Turns the generator's plain-paragraph answer text (app/prompts.py rule 6 forbids headings and
// heavy bold, so paragraphs on blank lines are the only structure there is) into the pieces
// Message.tsx renders: an optional large serif lead sentence, the remaining paragraphs, with the
// deterministic freshness-notice trailing paragraph removed when present.

import { citationRegex } from "./citations.ts";

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

// --- Markdown + citation rendering ------------------------------------------------------------
//
// The generator writes markdown (bold, italics, links, lists) despite app/prompts.py's plain-
// paragraph framing, and until now Message.tsx rendered every text run as a literal string, so
// readers saw "**30 days**" instead of a bold "30 days". Everything below builds React-renderable
// DATA (never HTML strings, never dangerouslySetInnerHTML) out of that markdown.
//
// This used to run as two sequential passes: lib/citations.ts::parseAnswerSegments split the
// whole string on citation markers first, and only the leftover plain-text runs were handed to
// this file's markdown parser. That broke the moment a markdown span CONTAINED a citation marker
// -- e.g. "*...extension [1].*" -- because the opening "*" and closing "*" landed in different
// text segments (split apart by the "[1]" segment between them), so neither half matched and both
// asterisks rendered as literal text. `parseInline` below is a SINGLE tokeniser over the whole
// string instead: it recognizes a citation marker as one more alternative alongside bold/italic/
// link, tried at every position in the same left-to-right scan, so a citation sitting inside a
// bold or italic span no longer splits the span's markers into different runs. `parseAnswerSegments`
// itself is untouched and still used elsewhere (see its own module and its test) -- this file just
// no longer depends on running after it.

/** One inline run: plain text, a citation marker, or one of the three inline markdown forms this
 * corpus's answers actually use (see the occurrence counts in the phase brief -- tables and code
 * spans do not occur and are deliberately not attempted here). `bold` and `italic` recurse so that
 * "**bold *and* italic**" nests correctly instead of being flattened to plain text, and so that
 * "**30 days [7]**" nests a `citation` node inside the `bold` node's children. */
export type InlineNode =
  | { kind: "text"; value: string }
  | { kind: "bold"; children: InlineNode[] }
  | { kind: "italic"; children: InlineNode[] }
  | { kind: "link"; label: string; url: string }
  | { kind: "citation"; indices: number[] };

// Only http(s) renders as a clickable anchor (constraint: a `javascript:` or other scheme must
// render as inert literal text, never an anchor).
const SAFE_URL_RE = /^https?:\/\//i;

// Tried left-to-right, first-alternative-wins at each position. The citation alternative is
// listed FIRST, before the link alternative, so a bare marker like "[7]" is always claimed by the
// citation branch rather than the link branch -- this is the same "citation markers must never be
// parsed as a markdown link" guarantee the old two-pass order used to provide, now expressed as
// alternative order within one pattern instead of as a separate earlier pass. Its source comes
// from lib/citations.ts::citationRegex() (spliced in via `.source`), so this pattern can never
// drift from the one true bracket convention lib/citations.ts documents.
//
// After the citation alternative, "**bold**" is claimed by the double-star alternative before the
// single-star italic alternative ever gets a chance at it. Each captured span requires at least
// one interior character, so every match consumes at least three characters and `lastIndex`
// always advances -- no zero-length-match infinite loop is possible. An unmatched or malformed
// marker (a stray "**", a link missing its closing paren) simply never matches anything here and
// falls through to the trailing plain-text slice untouched.
//
// This is a function, not a module-level RegExp constant, on purpose: `parseInline` recurses into
// bold/italic contents, and a `g`-flagged RegExp carries its scan position in mutable
// `lastIndex` state on the object itself. A single shared instance would have that state
// clobbered by the recursive call (which resets and advances `lastIndex` against the shorter
// inner string) out from under the outer loop's own `.exec` calls on the outer string, which is
// exactly the kind of non-terminating, ever-growing loop that once ran this process out of heap.
// A fresh RegExp per call keeps each call's scan position private to it.
function inlineRegex(): RegExp {
  const citationSource = citationRegex().source;
  return new RegExp(
    `${citationSource}|\\*\\*(.+?)\\*\\*|\\*(.+?)\\*|_(.+?)_|\\[([^[\\]]*)\\]\\(([^()]*)\\)`,
    "g",
  );
}

/** Parses `text` -- a full paragraph, list item, or lead sentence, citation markers and all --
 * into an ordered list of inline nodes. Every character of `text` is preserved in exactly one
 * node's `value`/`label`/`indices` or in the literal marker characters of an unmatched-and-thus-
 * literal span; nothing is dropped. Recursing into a `bold`/`italic` span's captured interior
 * calls this same function again, which is what lets a citation marker legally sit inside one. */
export function parseInline(text: string): InlineNode[] {
  const nodes: InlineNode[] = [];
  let lastIndex = 0;
  const re = inlineRegex();
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push({ kind: "text", value: text.slice(lastIndex, match.index) });
    }
    const [, citationIndices, bold, italicStar, italicUnderscore, linkLabel, linkUrl] = match;
    if (citationIndices !== undefined) {
      const indices = citationIndices.split(",").map((part) => Number.parseInt(part.trim(), 10));
      nodes.push({ kind: "citation", indices });
    } else if (bold !== undefined) {
      nodes.push({ kind: "bold", children: parseInline(bold) });
    } else if (italicStar !== undefined) {
      nodes.push({ kind: "italic", children: parseInline(italicStar) });
    } else if (italicUnderscore !== undefined) {
      nodes.push({ kind: "italic", children: parseInline(italicUnderscore) });
    } else if (linkLabel !== undefined && linkUrl !== undefined && SAFE_URL_RE.test(linkUrl)) {
      nodes.push({ kind: "link", label: linkLabel, url: linkUrl });
    } else {
      // A `[label](url)` whose url failed the scheme check: render the whole thing literally,
      // unchanged, rather than as a link.
      nodes.push({ kind: "text", value: match[0] });
    }
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    nodes.push({ kind: "text", value: text.slice(lastIndex) });
  }
  return nodes;
}

/** One block-level unit inside a single blank-line-delimited paragraph (see `splitParagraphs`).
 * `bullet-list` and `numbered-list` carry raw item text, each still to be run through citation
 * parsing and `parseInline` by the caller -- exactly the same as any other paragraph text. */
export type Block =
  | { kind: "paragraph"; text: string }
  | { kind: "heading"; text: string }
  | { kind: "bullet-list"; items: string[] }
  | { kind: "numbered-list"; items: string[] };

const HEADING_RE = /^#{1,6}\s+(.*)$/;
const BULLET_RE = /^[-*•]\s+(.*)$/;
const NUMBERED_RE = /^\d+\.\s+(.*)$/;

/** Splits one paragraph's lines into blocks. `splitParagraphs` already isolated blank-line-
 * delimited paragraphs; this handles the case within a single paragraph where the model separates
 * list items with single newlines (not blank lines), which otherwise renders as one run-on
 * paragraph. Consecutive plain-text lines are rejoined with a space (the same as a browser
 * collapses an embedded newline in normal prose, so this is not a visible change for non-list
 * paragraphs); consecutive bullet or numbered lines become one list block each. */
export function parseBlocks(paragraph: string): Block[] {
  const lines = paragraph
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

  const blocks: Block[] = [];
  let paragraphLines: string[] = [];
  let listItems: string[] = [];
  let listKind: "bullet-list" | "numbered-list" | null = null;

  const flushParagraph = () => {
    if (paragraphLines.length > 0) {
      blocks.push({ kind: "paragraph", text: paragraphLines.join(" ") });
      paragraphLines = [];
    }
  };
  const flushList = () => {
    if (listKind && listItems.length > 0) {
      blocks.push({ kind: listKind, items: listItems });
    }
    listItems = [];
    listKind = null;
  };

  for (const line of lines) {
    const heading = line.match(HEADING_RE);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ kind: "heading", text: heading[1] });
      continue;
    }
    const bullet = line.match(BULLET_RE);
    if (bullet) {
      flushParagraph();
      if (listKind !== "bullet-list") flushList();
      listKind = "bullet-list";
      listItems.push(bullet[1]);
      continue;
    }
    const numbered = line.match(NUMBERED_RE);
    if (numbered) {
      flushParagraph();
      if (listKind !== "numbered-list") flushList();
      listKind = "numbered-list";
      listItems.push(numbered[1]);
      continue;
    }
    flushList();
    paragraphLines.push(line);
  }
  flushParagraph();
  flushList();
  return blocks;
}
