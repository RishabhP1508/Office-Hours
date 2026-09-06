import type { Citation, Freshness, RetrievedContext } from "../lib/api";
import { formatShortDate, hostnameOf, isSameCalendarDay } from "../lib/format";
import { withNonBreakingHyphens } from "../lib/nonbreaking";

const I_LINK = (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    className="mt-[2px] flex-shrink-0 text-link"
    aria-hidden="true"
  >
    <path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1" />
    <path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1" />
  </svg>
);

/** "Where this came from": one row per citation (not deduplicated by URL -- two citations from
 * the same page are two different retrieved chunks, each worth its own row and its own bracket
 * number), pairing each citation with that source's own freshness bookkeeping. Staleness is the
 * main risk in this domain, so page-updated/verified dates are shown plainly, never behind a
 * tooltip. */
export default function SourceList({
  citations,
  contexts,
  freshness,
}: {
  citations: Citation[];
  contexts: RetrievedContext[];
  freshness: Freshness | null;
}) {
  if (citations.length === 0) return null;

  return (
    <div className="mt-7 border-t border-rule pt-5">
      <h3 className="mb-3.5 text-[12.5px] font-medium text-muted">Where this came from</h3>
      {citations.map((citation, i) => {
        const index = i + 1;
        const context = contexts.find((c) => c.chunk_id === citation.chunk_id);
        const source = freshness?.sources.find((s) => s.source_url === citation.source_url);
        return (
          <a
            key={`${citation.chunk_id}-${i}`}
            href={citation.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="mb-3.5 flex items-start gap-2.5 no-underline"
          >
            {I_LINK}
            <span>
              <span className="block text-[15px] font-medium leading-[1.45] text-link">
                [{index}]{" "}
                {withNonBreakingHyphens(context?.section_heading ?? hostnameOf(citation.source_url))}
              </span>
              <span className="mt-1 block text-[12.5px] leading-[1.6] text-muted">
                {hostnameOf(citation.source_url)}
                {source?.page_last_updated && (
                  <> · page updated {formatShortDate(source.page_last_updated)}</>
                )}
                {source && freshness && (
                  <>
                    {" "}
                    · verified{" "}
                    {isSameCalendarDay(source.last_verified_at, freshness.as_of)
                      ? "today"
                      : formatShortDate(source.last_verified_at)}
                  </>
                )}
              </span>
            </span>
          </a>
        );
      })}
    </div>
  );
}
