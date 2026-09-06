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

function NoAnswer({ question, response }: { question: string; response: AnswerResponse }) {
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
      <Handoff heading="Where to look instead">
        This falls outside the <span className="whitespace-nowrap">F‑1</span>, OPT, STEM OPT, and{" "}
        <span className="whitespace-nowrap">H‑1B</span> sources this tool has indexed. Your DSO has
        likely answered this question before, and a licensed immigration attorney can help with
        anything specific to your case.
      </Handoff>
      <Stamp response={response} extra="no sources cited because none applied" />
    </div>
  );
}

function BlockedUnverified({ question, response }: { question: string; response: AnswerResponse }) {
  // Deliberately plain text, never CitedProse: the fixed safe message never carries a bracket, so
  // running it through the citation parser would only risk treating stray punctuation as one.
  // withNonBreakingHyphens is still safe and applied: it only ever substitutes a character inside
  // an already-matched form-number/status-code token, so it can never introduce or remove a
  // bracket the citation parser would have to see.
  return (
    <div>
      <YouEcho question={question} />
      <p className="font-serif text-lg leading-[1.85] text-body">
        {withNonBreakingHyphens(response.answer)}
      </p>
      <Handoff heading="This answer didn't pass our citation check">
        Every claim this tool shows has to trace back to a source it actually retrieved, and this
        one didn’t clear that bar, so it’s withheld rather than shown. Try rephrasing the question.
      </Handoff>
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
