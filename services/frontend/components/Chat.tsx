"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { streamQuery, type AnswerResponse, type StageEvent } from "../lib/api";
import { MOCK_QUESTIONS, MOCK_RESPONSES } from "../lib/fixtures";
import { withNonBreakingHyphens } from "../lib/nonbreaking";
import { buildSourceCards } from "../lib/sources";
import Handoff from "./Handoff";
import Hero from "./Hero";
import Message from "./Message";
import Progress from "./Progress";
import { SourceRail, SourceSheet, SourcesProvider } from "./SourceList";

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

// Three example chips, each demonstrating a different response_type against the real corpus
// (verified against the live stack while building this phase): a plain factual question, a
// personal-decision question that gets the general rule plus a handoff, and a question this
// corpus genuinely does not cover.
const EXAMPLES: { question: string; state: string }[] = [
  { question: "How long is the STEM OPT extension?", state: "answer" },
  { question: "My OPT ends in July but my H-1B starts in October. Am I covered?", state: "advice" },
  { question: "How do I make sourdough bread rise properly?", state: "no source" },
];

type ViewState =
  | { phase: "home" }
  | { phase: "loading"; question: string; events: StageEvent[]; startedAt: number }
  | { phase: "done"; question: string; response: AnswerResponse }
  | { phase: "error"; question: string; detail: string };

/** The very first render (SSR, and the client's first paint before hydration's effects run) --
 * derived synchronously from whatever `q`/`mock` the URL already carries, so a cold load of a
 * shared `/?q=...` link shows the real loading skeleton immediately rather than flashing the home
 * screen first. Every render after this one is kept in sync by the effect in `Chat` below, which
 * is the only place that actually starts a fetch or reads the cache. */
function computeInitialView(searchParams: URLSearchParams): ViewState {
  const mockState = searchParams.get("mock");
  const mockResponse = mockState ? MOCK_RESPONSES[mockState] : undefined;
  if (mockState && mockResponse) {
    return { phase: "done", question: MOCK_QUESTIONS[mockState], response: mockResponse };
  }
  const q = searchParams.get("q");
  if (q) {
    return { phase: "loading", question: q, events: [], startedAt: Date.now() };
  }
  return { phase: "home" };
}

function FollowUpAsk({
  placeholder,
  autoFocus,
  onSubmit,
}: {
  placeholder: string;
  autoFocus: boolean;
  onSubmit: (question: string) => void;
}) {
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  return (
    <form
      className="mt-6 flex items-center gap-2.5 rounded-md border-[1.5px] border-rule py-[5px] pl-4 pr-[5px] focus-within:border-saffron"
      onSubmit={(e) => {
        e.preventDefault();
        const trimmed = value.trim();
        if (!trimmed) return;
        onSubmit(trimmed);
        setValue("");
      }}
    >
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={placeholder}
        aria-label={placeholder}
        className="min-w-0 flex-1 bg-transparent py-2.5 text-base text-ink placeholder:text-[#9C8C7A] focus:outline-none"
      />
      <button
        type="submit"
        aria-label="Send question"
        className="flex h-[38px] w-[38px] flex-shrink-0 items-center justify-center rounded bg-saffron hover:bg-[#C4711A]"
      >
        {I_UP}
      </button>
    </form>
  );
}

function Sheet({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-auto max-w-[800px] px-7">
      <div className="my-8 rounded-lg border border-rule bg-paper p-8">{children}</div>
    </div>
  );
}

export default function Chat() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const q = searchParams.get("q");
  const mockState = searchParams.get("mock");

  // question -> response, for this browser session only (a plain in-memory Map, not persisted
  // anywhere). Lets browser back/forward restore a previously answered question instantly instead
  // of re-querying it. A cold load of /?q=... (a shared link) never has an entry here on its first
  // render, and correctly falls through to a real query -- see the effect below.
  const cache = useRef(new Map<string, AnswerResponse>());

  const [view, setView] = useState<ViewState>(() => computeInitialView(searchParams));

  // The single source of truth for what `view` is: reacts to `q`/`mock` (the URL), never the
  // other way around. Every navigation this app performs -- a new submission, browser back/
  // forward, the wordmark going home, changing ?mock= -- is a URL change, and this effect is what
  // turns that URL change into the right view. Its cleanup (`controller.abort()`) is what aborts
  // an in-flight request the moment the URL moves on to something else, whether that "something
  // else" is a brand new question or the user going home.
  useEffect(() => {
    const mockResponse = mockState ? MOCK_RESPONSES[mockState] : undefined;
    if (mockState && mockResponse) {
      setView({ phase: "done", question: MOCK_QUESTIONS[mockState], response: mockResponse });
      return;
    }

    if (!q) {
      setView({ phase: "home" });
      return;
    }

    const cached = cache.current.get(q);
    if (cached) {
      setView({ phase: "done", question: q, response: cached });
      return;
    }

    const controller = new AbortController();
    // Renders the stage skeleton and the echoed question on this render, before the network call
    // below even starts -- see CLAUDE.md's "something informative within 2 seconds" requirement.
    setView({ phase: "loading", question: q, events: [], startedAt: Date.now() });

    streamQuery(
      q,
      (event) => {
        if (controller.signal.aborted) return;
        if (event.event === "result") {
          cache.current.set(q, event.response);
          setView({ phase: "done", question: q, response: event.response });
        } else if (event.event === "error") {
          setView({ phase: "error", question: q, detail: event.detail });
        } else if (event.event === "stage") {
          setView((prev) =>
            prev.phase === "loading" && prev.question === q
              ? { ...prev, events: [...prev.events, event] }
              : prev
          );
        }
      },
      controller.signal
    ).catch((err: unknown) => {
      if (controller.signal.aborted) return;
      setView({
        phase: "error",
        question: q,
        detail: err instanceof Error ? err.message : String(err),
      });
    });

    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reacts to the URL only, not `view`
  }, [q, mockState]);

  const submit = (question: string) => {
    router.push(`/?q=${encodeURIComponent(question)}`);
  };

  if (view.phase === "home") {
    return (
      <div>
        <Hero onAsk={submit} />
        <div className="border-b border-rule bg-paper">
          <div className="mx-auto flex max-w-[1120px] flex-wrap items-center gap-3 px-7 py-[17px]">
            <span className="flex-shrink-0 text-[13px] text-muted">Try one of these</span>
            {EXAMPLES.map((example) => (
              <button
                key={example.question}
                type="button"
                onClick={() => submit(example.question)}
                className="rounded-full border border-rule bg-page px-[15px] py-2 text-[13.5px] text-ink hover:border-saffron hover:text-link"
              >
                {withNonBreakingHyphens(example.question)}
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (view.phase === "loading") {
    return (
      <Sheet>
        <Progress question={withNonBreakingHyphens(view.question)} events={view.events} startedAt={view.startedAt} />
      </Sheet>
    );
  }

  if (view.phase === "error") {
    return (
      <Sheet>
        <div className="mb-7 rounded-md bg-page px-[18px] py-3.5 text-[15.5px] leading-[1.6] text-body">
          {withNonBreakingHyphens(view.question)}
        </div>
        <Handoff heading="That request didn't go through">
          {view.detail || "The connection to the server was interrupted."} Try asking again.
        </Handoff>
        <FollowUpAsk placeholder="Ask again" autoFocus={false} onSubmit={submit} />
      </Sheet>
    );
  }

  // "Follow-up" is never used here: this system has no conversation memory at all -- every
  // question is answered from scratch, with no continuity from whatever was asked before -- so no
  // placeholder may imply otherwise. "Ask something else" already reads that way for the two
  // states where the previous attempt did not produce a usable answer; "Ask another question" says
  // the same thing for a normal answer or refusal.
  const followUpPlaceholder =
    view.response.response_type === "clarify"
      ? "Ask about your status"
      : view.response.response_type === "no_answer" ||
          view.response.response_type === "blocked_unverified"
        ? "Ask something else"
        : "Ask another question";

  const cards =
    view.response.response_type === "answer" || view.response.response_type === "refusal_advice"
      ? buildSourceCards(view.response)
      : [];

  if (cards.length === 0) {
    return (
      <Sheet>
        <Message question={view.question} response={view.response} />
        <FollowUpAsk
          placeholder={followUpPlaceholder}
          autoFocus={view.response.response_type === "clarify"}
          onSubmit={submit}
        />
      </Sheet>
    );
  }

  // The `key` clears the highlighted marker/card and closes the sheet whenever the view changes:
  // a new question (or a new ?mock=) remounts SourcesProvider from scratch, rather than carrying
  // stale selection state from the previous answer into this one.
  return (
    <SourcesProvider key={`${mockState ?? ""}|${q ?? ""}`} cards={cards}>
      <div className="mx-auto max-w-[800px] px-7 rail:max-w-[1160px]">
        <div className="grid grid-cols-[minmax(0,1fr)] items-start justify-center gap-[30px] rail:grid-cols-[minmax(0,760px)_340px]">
          <div className="my-8 rounded-lg border border-rule bg-paper p-8">
            <Message question={view.question} response={view.response} />
            <FollowUpAsk
              placeholder={followUpPlaceholder}
              autoFocus={view.response.response_type === "clarify"}
              onSubmit={submit}
            />
          </div>
          <SourceRail />
        </div>
      </div>
      <SourceSheet />
    </SourcesProvider>
  );
}
