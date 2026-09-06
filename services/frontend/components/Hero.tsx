"use client";

import { useState } from "react";
import { MOCK_ANSWER } from "../lib/fixtures";
import { formatShortDate, hostnameOf, isSameCalendarDay } from "../lib/format";
import Citation from "./Citation";

const I_CAP = (
  <svg
    width="17"
    height="17"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.7"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M22 9 12 5 2 9l10 4 10-4Z" />
    <path d="M6 11v5c0 1 2.7 2.5 6 2.5s6-1.5 6-2.5v-5" />
  </svg>
);

const I_UP = (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#2B1F12"
    strokeWidth="2.2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M12 19V5M6 11l6-6 6 6" />
  </svg>
);

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

// The demo card beside the hero copy is a real cited answer, not an invented example: the
// citation, its source URL, and the freshness dates all come from MOCK_ANSWER (lib/fixtures.ts),
// captured from the live orchestrator answering "How long is the STEM OPT extension?" against the
// real corpus. "24 months" is that same answer's own headline fact, in the short, high-contrast
// shape the settled design calls for on the home screen specifically.
const DEMO_QUESTION = "How long is the STEM OPT extension?";
const demoSource = MOCK_ANSWER.citations[0];
const demoFreshnessSource = MOCK_ANSWER.freshness?.sources.find(
  (s) => s.source_url === demoSource.source_url
);

export default function Hero({ onAsk }: { onAsk: (question: string) => void }) {
  const [value, setValue] = useState("");

  const submit = () => {
    const trimmed = value.trim();
    if (trimmed) onAsk(trimmed);
  };

  return (
    <div className="bg-espresso pb-16">
      <div className="mx-auto max-w-[1120px] px-7">
        <div className="grid grid-cols-1 items-center gap-8 pt-9 min-[900px]:grid-cols-[1.05fr_.95fr] min-[900px]:gap-[52px]">
          <div>
            <h1 className="m-0 mb-[18px] font-serif text-[29px] font-normal leading-[1.2] tracking-tight text-[#FBF6EF] min-[900px]:text-[35px] min-[1120px]:text-[44px] min-[1120px]:leading-[1.16]">
              Cited answers on <span className="whitespace-nowrap">F‑1</span>, OPT, and{" "}
              <span className="whitespace-nowrap">H‑1B</span>
            </h1>
            <p className="mb-7 max-w-[42ch] text-[16px] leading-[1.75] text-[#C0B1A0] min-[640px]:text-[17px]">
              Every answer links to the official government page it came from. When the sources do
              not cover something, it says so instead of guessing.
            </p>
            <form
              className="flex items-center gap-2.5 rounded-md bg-paper py-[5px] pl-[18px] pr-[5px]"
              onSubmit={(e) => {
                e.preventDefault();
                submit();
              }}
            >
              <input
                type="text"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                placeholder="Ask about your status"
                aria-label="Ask a question"
                className="min-w-0 flex-1 bg-transparent py-3 text-base text-ink placeholder:text-[#9C8C7A] focus:outline-none"
              />
              <button
                type="submit"
                aria-label="Send question"
                className="flex h-[42px] w-[42px] flex-shrink-0 items-center justify-center rounded bg-saffron hover:bg-[#C4711A]"
              >
                {I_UP}
              </button>
            </form>
            <span className="mt-5 inline-flex items-center gap-[9px] text-[13.5px] text-saffron-lit">
              {I_CAP}
              <span>
                Built by an international student on <span className="whitespace-nowrap">STEM OPT</span>
              </span>
            </span>
          </div>

          <div className="rounded-lg bg-paper p-[26px] shadow-[0_16px_40px_rgba(20,12,4,.38)] min-[900px]:p-[22px_20px] min-[1120px]:p-[26px_28px]">
            <p className="m-0 mb-4 border-b border-rule pb-[15px] text-sm text-muted">
              {DEMO_QUESTION}
            </p>
            <span className="mb-[11px] block font-serif text-xl font-semibold text-ink min-[640px]:text-[23px]">
              24 months.
            </span>
            <p className="m-0 mb-[18px] font-serif text-base leading-[1.78] text-body">
              If your degree is on the STEM list and your employer uses{" "}
              <span className="whitespace-nowrap">E‑Verify</span>, you can add 24 months on top of
              your first 12 months of OPT
              <Citation indices={[1]} citations={MOCK_ANSWER.citations} />.
            </p>
            <div className="flex items-start gap-2.5 border-t border-rule pt-[15px]">
              {I_LINK}
              <div>
                <span className="block text-[13px] font-medium leading-[1.5] text-link">
                  {MOCK_ANSWER.contexts.find((c) => c.chunk_id === demoSource.chunk_id)
                    ?.section_heading ?? "STEM OPT extension"}
                </span>
                <span className="text-[12px] leading-[1.6] text-muted">
                  {hostnameOf(demoSource.source_url)}
                  {demoFreshnessSource?.page_last_updated && (
                    <> · page updated {formatShortDate(demoFreshnessSource.page_last_updated)}</>
                  )}
                  {demoFreshnessSource && MOCK_ANSWER.freshness && (
                    <>
                      {" "}
                      · verified{" "}
                      {isSameCalendarDay(
                        demoFreshnessSource.last_verified_at,
                        MOCK_ANSWER.freshness.as_of
                      )
                        ? "today"
                        : formatShortDate(demoFreshnessSource.last_verified_at)}
                    </>
                  )}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
