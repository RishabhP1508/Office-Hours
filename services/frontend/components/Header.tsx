"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { getSourcesStatus, type SourcesStatus } from "../lib/api";
import { formatDayMonthYear } from "../lib/format";
import Disclaimer from "./Disclaimer";

interface FreshnessDisplay {
  /** Full string, shown at the `sm` breakpoint and above. */
  label: string;
  /** Terser string for below `sm`: "Sources last checked 22 Aug 2026" measures 194px, and the row
   * has only ~116px free for it at a 375px content width once the wordmark, the tag, and their
   * gaps are accounted for -- the full string simply does not fit there. Un-hiding the full string
   * at mobile widths would still leave the indicator invisible, the same silent-staleness failure
   * this whole indicator exists to prevent, so a real second string is required, not a CSS tweak. */
  terseLabel: string;
  showDot: boolean;
}

/** What the freshness indicator shows, derived ONLY from `freshness_state` -- the backend
 * (app/guardrails/freshness.py::sources_freshness_state) is the one place that band decision is
 * computed; this function must never recompute a freshness claim of its own from raw dates. A
 * saffron dot means "no need to double-check this"; dropping it for "stale" (rather than
 * recolouring it) avoids introducing a third hue the palette forbids, and a saffron dot next to a
 * stale date would read as a false all-clear. */
function freshnessDisplay(status: SourcesStatus): FreshnessDisplay | null {
  switch (status.freshness_state) {
    case "current":
      return { label: "Sources checked today", terseLabel: "Checked today", showDot: true };
    case "recent": {
      const days = status.age_hours !== null ? Math.max(1, Math.floor(status.age_hours / 24)) : 1;
      const agoPhrase = `${days} day${days === 1 ? "" : "s"} ago`;
      // Below `sm`, this one drops "Checked " entirely (not just the gap trim below) -- "Checked
      // N days ago" does not fit the ~116px free at a 375px content width for any N from 1 to 7,
      // and the bare "N days ago" is what actually holds the whole band on one line, measured at
      // every N from 1 to 7 in a real browser. The full label at `sm` and above is unaffected.
      return { label: `Sources checked ${agoPhrase}`, terseLabel: agoPhrase, showDot: true };
    }
    case "stale": {
      if (!status.oldest_verified_at) return null;
      const date = formatDayMonthYear(status.oldest_verified_at);
      return { label: `Sources last checked ${date}`, terseLabel: `Checked ${date}`, showDot: false };
    }
    case "unknown":
    default:
      return null;
  }
}

/** The header band: a sticky slim top row (wordmark, "Unofficial" tag, live sources indicator)
 * plus the standing disclaimer band underneath, which is NOT sticky and scrolls away normally. The
 * indicator's text is read from GET /sources/status -- it must never display a claim the database
 * does not back -- and renders nothing until the real status has loaded, rather than guessing. */
export default function Header() {
  const router = useRouter();
  const [status, setStatus] = useState<SourcesStatus | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getSourcesStatus(controller.signal)
      .then(setStatus)
      .catch(() => setStatus(null));
    return () => controller.abort();
  }, []);

  const display = status ? freshnessDisplay(status) : null;

  return (
    <>
      <div className="sticky top-0 z-40 bg-espresso">
        <div className="mx-auto flex max-w-[1120px] flex-wrap items-center gap-3 px-7 py-4">
          <button
            type="button"
            onClick={() => router.push("/")}
            aria-label="Office Hours home"
            className="font-serif text-xl font-semibold tracking-tight text-[#FBF6EF]"
          >
            Office Hours
          </button>
          {/* flex-shrink-0 + whitespace-nowrap make the "never shrink, never wrap" priority
              explicit in code, rather than relying on a flex item's default min-width: auto --
              that default is what has been protecting this tag, undeclared, and a future
              min-w-0 or a longer tag string would silently remove it. Under width pressure the
              wordmark yields; this tag never does. */}
          <span className="flex-shrink-0 whitespace-nowrap rounded border border-[#46372A] px-[7px] py-[2px] text-[11px] text-[#B5A796]">
            Unofficial
          </span>
          {display && (
            <span className="ml-auto flex items-center gap-1 text-[12.5px] text-[#B5A796] sm:gap-[7px]">
              {display.showDot && (
                <span className="h-[6px] w-[6px] flex-shrink-0 rounded-full bg-saffron-lit" />
              )}
              <span className="hidden sm:inline">{display.label}</span>
              <span className="inline sm:hidden">{display.terseLabel}</span>
            </span>
          )}
        </div>
      </div>
      <div className="bg-espresso">
        <Disclaimer />
      </div>
    </>
  );
}
