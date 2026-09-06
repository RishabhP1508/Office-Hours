import type { Citation as CitationType } from "../lib/api";

/** One bracket citation marker (`[2]`, or a comma group like `[1, 3]`), parsed by
 * lib/citations.ts::parseAnswerSegments. Each number links to `citations[n-1].source_url`; an
 * index with no matching citation (should never happen once app/guardrails/citations.py has
 * verified the answer, but a defensive frontend never trusts that blindly) renders as inert text,
 * never a broken link. Brackets are never stripped, renumbered, or reformatted. */
export default function Citation({
  indices,
  citations,
}: {
  indices: number[];
  citations: CitationType[];
}) {
  return (
    <span className="whitespace-nowrap">
      [
      {indices.map((index, i) => {
        const citation = citations[index - 1];
        return (
          <span key={`${index}-${i}`}>
            {i > 0 && ", "}
            {citation ? (
              <a
                href={citation.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="align-[2px] px-px text-[12px] font-medium text-link no-underline hover:underline"
              >
                {index}
              </a>
            ) : (
              <span className="align-[2px] px-px text-[12px] font-medium text-muted">
                {index}
              </span>
            )}
          </span>
        );
      })}
      ]
    </span>
  );
}
