import type { AnswerResponse, Citation as CitationType } from "../lib/api";
import { parseAnswerSegments } from "../lib/citations";
import { formatLongDate } from "../lib/format";
import { withNonBreakingHyphens } from "../lib/nonbreaking";
import { deriveLead, splitParagraphs, withoutDuplicateFreshnessNoticeParagraph } from "../lib/prose";
import Citation from "./Citation";
import Freshness from "./Freshness";
import Handoff from "./Handoff";
import SourceList from "./SourceList";

function CitedProse({ text, citations }: { text: string; citations: CitationType[] }) {
  const segments = parseAnswerSegments(withNonBreakingHyphens(text));
  return (
    <>
      {segments.map((segment, i) =>
        segment.kind === "text" ? (
          <span key={i}>{segment.value}</span>
        ) : (
          <Citation key={i} indices={segment.indices} citations={citations} />
        )
      )}
    </>
  );
}

function YouEcho({ question }: { question: string }) {
  return (
    <div className="mb-7 rounded-md bg-page px-[18px] py-3.5 text-[15.5px] leading-[1.6] text-body">
      {withNonBreakingHyphens(question)}
    </div>
  );
}

function Stamp({ response, extra }: { response: AnswerResponse; extra?: string }) {
  return (
    <p className="mt-1 text-[12.5px] text-muted">
      Answered {formatLongDate(response.generated_at)}
      {extra ? ` · ${extra}` : ""}
    </p>
  );
}

/** A cited factual answer, or a refusal that states the general rule before handing off -- both
 * render the same prose+citations+sources shape; only refusal_advice adds a handoff block. */
function AnsweredProse({
  question,
  response,
  isAdvice,
}: {
  question: string;
  response: AnswerResponse;
  isAdvice: boolean;
}) {
  const rawParagraphs = splitParagraphs(response.answer);
  const paragraphs = response.freshness
    ? withoutDuplicateFreshnessNoticeParagraph(rawParagraphs)
    : rawParagraphs;
  const { lead, paragraphs: bodyParagraphs } = deriveLead(paragraphs);

  return (
    <div>
      <YouEcho question={question} />
      <div className="max-w-[62ch] font-serif text-lg leading-[1.85] text-body">
        {lead && (
          <span className="mb-[15px] block text-[23px] font-semibold leading-[1.3] text-ink">
            <CitedProse text={lead} citations={response.citations} />
          </span>
        )}
        {bodyParagraphs.map((paragraph, i) => (
          <p key={i} className="mb-4 mt-0">
            <CitedProse text={paragraph} citations={response.citations} />
          </p>
        ))}
      </div>
      {response.freshness && <Freshness notices={response.freshness.notices} />}
      {isAdvice && (
        <Handoff heading="Your situation needs a person, not a page">
          The details that decide this are yours: your dates, your filings, your circumstances.
          Your DSO can look at those with you, at no cost, and a licensed immigration attorney can
          take it further if you need that.
        </Handoff>
      )}
      <SourceList
        citations={response.citations}
        contexts={response.contexts}
        freshness={response.freshness}
      />
      <Stamp response={response} />
    </div>
  );
}

function Clarify({ question, response }: { question: string; response: AnswerResponse }) {
  return (
    <div>
      <YouEcho question={question} />
      <p className="font-serif text-lg leading-[1.85] text-body">
        {withNonBreakingHyphens(response.answer)}
      </p>
    </div>
  );
}

// Copy for each known `refusal_reason` a NO_ANSWER response can carry, the same by-reason
// selection pattern _BLOCKED_COPY_BY_REASON below uses for BLOCKED_UNVERIFIED. The existing
// "outside the indexed sources" copy stays the default for every reason not listed here,
// including the ordinary "sources don't cover this" case and the daily-generation-cap
// degradation -- it was the only copy this component had before the stopgap below existed, and
// it is still correct for both of those.
//
// "non_latin_script_unsupported" (STOPGAP, 2026-09-08, app/pipeline.py -- see the large comment
// above _is_predominantly_non_latin there, and docs/adr/0018-non-latin-script-no-answer-
// stopgap.md) needs DIFFERENT copy, not the default: the default's "This falls outside the
// sources this tool has indexed" would be a FALSE statement here. The sources may well cover the
// question; the gate fired because the tool cannot read the question's script, before retrieval
// ever ran, not because retrieval came back empty.
const _DEFAULT_NO_ANSWER_COPY = {
  heading: "Where to look instead",
  body: (
    <>
      This falls outside the <span className="whitespace-nowrap">F‑1</span>, OPT, STEM OPT, and{" "}
      <span className="whitespace-nowrap">H‑1B</span> sources this tool has indexed. Your DSO has
      likely answered this question before, and a licensed immigration attorney can help with
      anything specific to your case.
    </>
  ),
  stampExtra: "no sources cited because none applied",
};

const _NO_ANSWER_COPY_BY_REASON: Record<
  string,
  { heading: string; body: React.ReactNode; stampExtra: string }
> = {
  non_latin_script_unsupported: {
    heading: "This tool only reads English right now",
    body: (
      <>
        The sources behind Office Hours may well cover your question. The problem is this tool
        can’t yet read it reliably in this language, and guessing risked handing you a confident
        but wrong number pointed at English sources you couldn’t easily check yourself. Try asking
        in English, or talk to your DSO or a licensed immigration attorney, who can help you in
        your own language.
      </>
    ),
    stampExtra: "no sources checked because the question couldn’t be read",
  },
};

function NoAnswer({ question, response }: { question: string; response: AnswerResponse }) {
  const paragraphs = splitParagraphs(response.answer);
  const copy =
    (response.refusal_reason && _NO_ANSWER_COPY_BY_REASON[response.refusal_reason]) ||
    _DEFAULT_NO_ANSWER_COPY;
  return (
    <div>
      <YouEcho question={question} />
      <div className="font-serif text-lg leading-[1.85] text-body">
        {paragraphs.map((paragraph, i) => (
          <p key={i} className="mb-4 mt-0">
            {withNonBreakingHyphens(paragraph)}
          </p>
        ))}
      </div>
      <Handoff heading={copy.heading}>{copy.body}</Handoff>
      <Stamp response={response} extra={copy.stampExtra} />
    </div>
  );
}

// Copy for each known `refusal_reason` a BLOCKED_UNVERIFIED response can carry (see
// app/pipeline.py::_blocked_message_for_reason, the backend's own by-reason selection). The
// citation-check copy stays the default for any reason not listed here, including
// "citation_index_out_of_range" and "answer_missing_citation" -- it was the only copy this
// component had before the authority guard existed, and it is still correct for both of those.
const _DEFAULT_BLOCKED_COPY = {
  heading: "This answer didn't pass our citation check",
  body: (
    <>
      Every claim this tool shows has to trace back to a source it actually retrieved, and this
      one didn’t clear that bar, so it’s withheld rather than shown. Try rephrasing the question.
    </>
  ),
};

const _BLOCKED_COPY_BY_REASON: Record<string, { heading: string; body: React.ReactNode }> = {
  answer_claims_official_authority: {
    heading: "This answer claimed to be official, so we blocked it",
    body: (
      <>
        This tool is not USCIS, DHS, ICE, or SEVP, and it is not authorized to speak for them or
        to give legal advice. The answer we generated crossed that line, so we are withholding it
        instead of showing it. Try rephrasing the question.
      </>
    ),
  },
};

function BlockedUnverified({ question, response }: { question: string; response: AnswerResponse }) {
  // Deliberately plain text, never CitedProse: the fixed safe message never carries a bracket, so
  // running it through the citation parser would only risk treating stray punctuation as one.
  // withNonBreakingHyphens is still safe and applied: it only ever substitutes a character inside
  // an already-matched form-number/status-code token, so it can never introduce or remove a
  // bracket the citation parser would have to see.
  const copy =
    (response.refusal_reason && _BLOCKED_COPY_BY_REASON[response.refusal_reason]) ||
    _DEFAULT_BLOCKED_COPY;
  return (
    <div>
      <YouEcho question={question} />
      <p className="font-serif text-lg leading-[1.85] text-body">
        {withNonBreakingHyphens(response.answer)}
      </p>
      <Handoff heading={copy.heading}>{copy.body}</Handoff>
      <Stamp response={response} />
    </div>
  );
}

function UnknownResponseType({ question, response }: { question: string; response: AnswerResponse }) {
  const paragraphs = splitParagraphs(response.answer);
  return (
    <div>
      <YouEcho question={question} />
      <div className="font-serif text-lg leading-[1.85] text-body">
        {paragraphs.map((paragraph, i) => (
          <p key={i} className="mb-4 mt-0">
            {withNonBreakingHyphens(paragraph)}
          </p>
        ))}
      </div>
    </div>
  );
}

/** Switches on `response_type`. Never infers which of the five states this is from the prose --
 * the pipeline's own decision, read directly off the wire, is what decides the layout. */
export default function Message({
  question,
  response,
}: {
  question: string;
  response: AnswerResponse;
}) {
  switch (response.response_type) {
    case "answer":
      return <AnsweredProse question={question} response={response} isAdvice={false} />;
    case "refusal_advice":
      return <AnsweredProse question={question} response={response} isAdvice={true} />;
    case "clarify":
      return <Clarify question={question} response={response} />;
    case "no_answer":
      return <NoAnswer question={question} response={response} />;
    case "blocked_unverified":
      return <BlockedUnverified question={question} response={response} />;
    default:
      return <UnknownResponseType question={question} response={response} />;
  }
}
