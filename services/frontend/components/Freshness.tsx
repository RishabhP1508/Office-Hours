import type { FreshnessNotice } from "../lib/api";
import { formatLongDate } from "../lib/format";

// Ordinal link labels for the multi-source case, matching the same convention
// app/guardrails/freshness.py::_join_source_links uses for its own generated prose ("the first
// source", "the second source", ...) -- independently applied here to the structured
// `freshness.notices` array, not by reading that backend prose. A bare hostname is not a safe
// label here: two distinct sources can share one domain (studyinthestates.dhs.gov hosts several
// of this corpus's fixed-admission pages), which would otherwise print the same label twice.
const ORDINALS = ["first", "second", "third", "fourth", "fifth"];

const I_CLOCK = (
  <svg
    width="15"
    height="15"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#B06A16"
    strokeWidth="1.9"
    strokeLinecap="round"
    className="mr-1.5 inline-block align-[-2px]"
    aria-hidden="true"
  >
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </svg>
);

interface NoticeGroup {
  date: string;
  inEffect: boolean;
  urls: string[];
}

// Collapses notices sharing one (rule_effective_date, in_effect) pair into one message with every
// source that stated it, mirroring app/guardrails/freshness.py::freshness_notice_text's own
// collapsing rule -- but derived here from the structured `freshness.notices` array itself, not
// by re-parsing that backend-generated prose.
function groupNotices(notices: FreshnessNotice[]): NoticeGroup[] {
  const groups = new Map<string, NoticeGroup>();
  for (const notice of notices) {
    const key = `${notice.rule_effective_date}|${notice.in_effect}`;
    const existing = groups.get(key);
    if (existing) {
      if (!existing.urls.includes(notice.source_url)) existing.urls.push(notice.source_url);
    } else {
      groups.set(key, {
        date: notice.rule_effective_date,
        inEffect: notice.in_effect,
        urls: [notice.source_url],
      });
    }
  }
  return Array.from(groups.values());
}

/** The dated-rule notice: its own sand block with a left saffron border and a clock icon. Wording
 * follows `in_effect` -- "took effect on" once the date has passed, "takes effect on" while it is
 * still ahead -- computed from the structured field, never scraped from prose. */
export default function Freshness({ notices }: { notices: FreshnessNotice[] }) {
  if (notices.length === 0) return null;

  return (
    <div className="my-5 border-l-[3px] border-saffron bg-sand px-[18px] py-3.5">
      {groupNotices(notices).map((group) => (
        <p
          key={`${group.date}-${group.inEffect}`}
          className="m-0 text-sm leading-[1.65] text-[#5C4526] first:mt-0 [&:not(:first-child)]:mt-2"
        >
          {I_CLOCK}
          {`A rule affecting this answer ${group.inEffect ? "took" : "takes"} effect on `}
          {formatLongDate(group.date)}
          {". "}
          {group.urls.map((url, i) => (
            <span key={url}>
              {i > 0 && ", "}
              <a href={url} target="_blank" rel="noopener noreferrer" className="font-medium">
                {group.urls.length === 1
                  ? "Read the source"
                  : `the ${ORDINALS[i] ?? "next"} source`}
              </a>
            </span>
          ))}
        </p>
      ))}
    </div>
  );
}
