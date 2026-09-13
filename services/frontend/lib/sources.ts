// One source of truth for the source-card data the desktop rail (SourceRail) and the mobile sheet
// (SourceSheet) both need. Pure data-shaping, no React: both surfaces call `buildSourceCards` and
// render the same array, so the rail and the sheet can never drift apart.
//
// The quote comes from the full chunk (`context.content`, via `quoteFromChunk`), not the
// 240-character `citation.snippet`: the breadcrumb line every chunk starts with can consume the
// entire snippet budget on pages whose section headings are entire questions (measured at 239 of
// 242 characters on the fixed-admission FAQ), leaving no snippet budget for prose at all.
// `quoteFromSnippet` stays as the fallback for when there is no matching context.

import type { AnswerResponse } from "./api.ts";
import { formatShortDate, hostnameOf, isSameCalendarDay } from "./format.ts";

export interface SourceCard {
  index: number; // 1-based citation number
  title: string; // context.section_heading ?? hostnameOf(url)
  quote: string; // verbatim run of the source's own prose (see quoteFromChunk, falls back to quoteFromSnippet)
  url: string; // citation.source_url
  domain: string; // hostnameOf(url)
  pageUpdated: string | null; // formatShortDate(source.page_last_updated), else null
  verified: string | null; // "today" if same calendar day as as_of, else formatShortDate(...); null with no freshness record
}

/**
 * The API's `citation.snippet` is the first 240 characters of the chunk, whitespace-collapsed, and
 * every chunk's content begins with a breadcrumb line -- either "<Doc title> > <Section heading>"
 * or just "<Doc title>" when the section heading equals the doc title. The card already prints the
 * section heading as its own title line, so an unstripped snippet repeats that title and then reads
 * on. This strips the breadcrumb (when present) so the quote is the page's own prose, not a repeat
 * of the title above it.
 *
 * Uses `indexOf`, never a regex built from the heading: headings contain `(`, `)`, `-`, and other
 * regex-meaningful characters that would need escaping for no benefit here.
 */
export function quoteFromSnippet(snippet: string, sectionHeading: string | null): string {
  if (!sectionHeading) return snippet.trim();

  const idx = snippet.indexOf(sectionHeading);
  if (idx === -1) return snippet.trim();

  // Require the breadcrumb shape: the heading sits at the very start of the snippet, or right
  // after a "> " separator. Anything else (the heading merely appearing mid-sentence) is not the
  // breadcrumb and must be left alone.
  const isBreadcrumbPosition = idx === 0 || snippet.slice(0, idx).endsWith("> ");
  if (!isBreadcrumbPosition) return snippet.trim();

  const rest = snippet.slice(idx + sectionHeading.length).trim();
  // Never hand back an empty or stub quote -- if stripping the breadcrumb would leave too little
  // prose behind (e.g. the snippet was truncated right after the heading), show the full snippet.
  if (rest.length < 40) return snippet.trim();

  return rest;
}

/**
 * The quote's real source: `context.content`, the FULL chunk, not the 240-character
 * `citation.snippet`. The snippet is too small an input on pages whose section headings are
 * entire questions -- on the fixed-admission FAQ, the breadcrumb alone is 239 characters of a
 * 242-character snippet (1.2% prose), so quoteFromSnippet's 40-char guard fires and the card
 * falls back to showing the breadcrumb itself as the "quote". The full chunk does not have that
 * problem: it is a chunk of body text of arbitrary length, so there is always prose past the
 * breadcrumb to draw from.
 *
 * The breadcrumb is always the chunk's first line, separated from the body by a blank line (see
 * app/pipeline.py's chunking), so this splits on the first blank-line boundary rather than
 * hunting for the heading text the way quoteFromSnippet must.
 */
export function quoteFromChunk(content: string): string {
  const boundary = content.match(/\n\s*\n/);
  if (!boundary || boundary.index === undefined) return "";

  const body = content.slice(boundary.index + boundary[0].length);
  if (!body.trim()) return "";

  // Markdown images are page decoration (icons), not prose -- rendered as literal alt-text +
  // URL by parseInline since they are not http(s) links, they are pure noise in a quote. Link
  // syntax, bold, and everything else are left untouched; the renderer handles those.
  const withoutImages = body.replace(/!\[[^\]]*\]\([^)]*\)/g, "");
  const collapsed = withoutImages.split(/\s+/).join(" ").trim();

  // Same threshold, same reason as quoteFromSnippet's guard: never hand back a stub.
  if (collapsed.length < 40) return "";

  if (collapsed.length <= 240) return collapsed;

  // Same 240-character budget and trailing ellipsis as the backend's own _snippet, so the rail
  // does not suddenly show much more text per card than before.
  return collapsed.slice(0, 240).replace(/\s+\S*$/, "") + "...";
}

/**
 * One card per citation, in order, NOT deduplicated by URL -- two citations from the same page are
 * two different retrieved chunks, each worth its own card and its own bracket number. Mirrors the
 * pairing logic that used to live directly in components/SourceList.tsx.
 */
export function buildSourceCards(response: AnswerResponse): SourceCard[] {
  return response.citations.map((citation, i) => {
    const index = i + 1;
    const context = response.contexts.find((c) => c.chunk_id === citation.chunk_id);
    const source = response.freshness?.sources.find((s) => s.source_url === citation.source_url);
    const sectionHeading = context?.section_heading ?? null;
    const fromChunk = context?.content ? quoteFromChunk(context.content) : "";

    return {
      index,
      title: context?.section_heading ?? hostnameOf(citation.source_url),
      quote: fromChunk || quoteFromSnippet(citation.snippet, sectionHeading),
      url: citation.source_url,
      domain: hostnameOf(citation.source_url),
      pageUpdated: source?.page_last_updated ? formatShortDate(source.page_last_updated) : null,
      verified:
        source && response.freshness
          ? isSameCalendarDay(source.last_verified_at, response.freshness.as_of)
            ? "today"
            : formatShortDate(source.last_verified_at)
          : null,
    };
  });
}
