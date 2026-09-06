/** The not-legal-advice banner. Single source of truth for the standing disclaimer in the header
 * band (rendered by components/Header.tsx) -- permanent on every page and every state, and never
 * a dismiss control. Names all three things CLAUDE.md requires: unofficial, not legal advice, not
 * affiliated with USCIS. Fixed text, not backend-supplied: `AnswerResponse.disclaimer` is no
 * longer rendered anywhere (this banner replaces it), so there is nothing here for the API to
 * drive. */
export default function Disclaimer() {
  return (
    <div className="border-t border-[#3A2E20] px-7 py-2">
      <p className="mx-auto max-w-[1120px] text-[12px] leading-[1.5] text-[#B5A796]">
        Unofficial tool. Not legal advice. Not affiliated with USCIS.
      </p>
    </div>
  );
}
