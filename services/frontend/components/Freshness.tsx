import type { FreshnessNotice } from "../lib/api";
import { groupFreshnessNotices } from "../lib/freshness";

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

/** The dated/contested-rule notice: its own sand block with a left saffron border and a clock
 * icon. Every wording decision (which verb, which link label, whether the maintainer attribution
 * appears, how notices group) lives in lib/freshness.ts -- this component renders the `parts`
 * array that function returns and makes NO copy decisions of its own. See that module's own
 * docstring for why: a `.tsx` file cannot be unit tested here without a new dependency, so the
 * string deciding force/status wording -- the exact thing that broke on 2026-09-19 -- must live
 * somewhere `node --test lib/*.test.ts` can actually exercise it. */
export default function Freshness({ notices }: { notices: FreshnessNotice[] }) {
  if (notices.length === 0) return null;

  return (
    <div className="my-5 border-l-[3px] border-saffron bg-sand px-[18px] py-3.5">
      {groupFreshnessNotices(notices).map((group) => (
        <p
          key={group.key}
          className="m-0 text-sm leading-[1.65] text-[#5C4526] first:mt-0 [&:not(:first-child)]:mt-2"
        >
          {I_CLOCK}
          {group.parts.map((part, i) =>
            part.kind === "text" ? (
              <span key={i}>{part.text}</span>
            ) : (
              <a
                key={i}
                href={part.url}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium"
              >
                {part.label}
              </a>
            )
          )}
        </p>
      ))}
    </div>
  );
}
