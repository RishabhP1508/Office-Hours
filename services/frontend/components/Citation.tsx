"use client";

import type { Citation as CitationType } from "../lib/api";
import { useSources } from "./SourceList";

/** One bracket citation marker (`[2]`, or a comma group like `[1, 3]`), parsed by
 * lib/prose.ts::parseInline using the pattern lib/citations.ts::citationRegex() defines. Each
 * marker is now an in-page control: clicking an index selects that source card in the desktop rail
 * (or opens the mobile sheet to it, when there is no visible rail), highlighting both the marker
 * and the card and scrolling the card into view. An index with no matching source card (should
 * never happen once app/guardrails/citations.py has verified the answer, but a defensive frontend
 * never trusts that blindly, and this is also the shape every marker takes when there is no
 * `SourcesProvider` above it at all -- e.g. `blocked_unverified`, whose citations are always `[]`)
 * renders as inert text, never a control that does nothing. Brackets are never stripped,
 * renumbered, or reformatted.
 *
 * Each index renders as a `<button>`, not an `<a href>`: measured in the browser, the old anchor
 * markup made the copy payload for `[1]` read as `[<span><a href="...">1</a></span>]`, so any paste
 * target that turns pasted HTML links into markdown wrote back `[[1](url)]` instead of `[1]`. A
 * button carries no href, so the same copy now yields a clean `[1]`. The outbound link itself is
 * not lost -- the source card's own title link (components/SourceList.tsx) still points at
 * `citation.source_url`. */
export default function Citation({
  indices,
  citations,
}: {
  indices: number[];
  citations: CitationType[];
}) {
  const sources = useSources();
  const anyActive = sources
    ? indices.some((index) => sources.activeIndex === index && sources.cards.some((c) => c.index === index))
    : false;

  return (
    <span className={`whitespace-nowrap ${anyActive ? "rounded-[3px] bg-saffron" : ""}`}>
      [
      {indices.map((index, i) => {
        const citation = citations[index - 1];
        const card = sources?.cards.find((c) => c.index === index);
        const isActive = sources?.activeIndex === index;
        return (
          <span key={`${index}-${i}`}>
            {i > 0 && ", "}
            {citation && sources && card ? (
              <button
                type="button"
                onClick={() => sources.select(index)}
                aria-label={`Show source ${index}`}
                className={`align-[2px] px-px text-[12px] font-medium no-underline hover:underline ${
                  isActive ? "text-[#2B1F12]" : "text-link"
                }`}
              >
                {index}
              </button>
            ) : (
              <span className="align-[2px] px-px text-[12px] font-medium text-muted">{index}</span>
            )}
          </span>
        );
      })}
      ]
    </span>
  );
}
