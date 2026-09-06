"use client";

import { useEffect, useState } from "react";
import type { StageEvent, StageName } from "../lib/api";

const I_TICK = (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#9A5312"
    strokeWidth="2.1"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m4 12 5 5L20 6" />
  </svg>
);

const I_DOT = (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#B3A899"
    strokeWidth="1.8"
    aria-hidden="true"
  >
    <circle cx="12" cy="12" r="4" />
  </svg>
);

const I_RUN = (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#241C13"
    strokeWidth="2.1"
    strokeLinecap="round"
    className="motion-safe:animate-spin"
    aria-hidden="true"
  >
    <circle cx="12" cy="12" r="8" opacity=".2" />
    <path d="M12 4a8 8 0 0 1 8 8" />
  </svg>
);

const STAGES: { name: StageName; label: string }[] = [
  { name: "classify", label: "Read your question" },
  { name: "retrieve", label: "Looking through official sources" },
  { name: "generate", label: "Writing the answer from those sources" },
  { name: "verify", label: "Checking every claim has a citation" },
];

type StageStatus = "wait" | "now" | "done";

function useElapsedSeconds(startedAt: number): number {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const tick = () => setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [startedAt]);
  return elapsed;
}

/** The waiting view. Every step and the fill percentage below are driven ONLY by real stage
 * events received over POST /query/stream -- never a setInterval-driven mock fill. The elapsed
 * counter is the one thing allowed to move on its own, because elapsed time is itself a real,
 * directly measured quantity, not a guess at how far along the pipeline is. */
export default function Progress({
  question,
  events,
  startedAt,
}: {
  question: string;
  events: StageEvent[];
  startedAt: number;
}) {
  const elapsed = useElapsedSeconds(startedAt);

  const statusOf = (stage: StageName): StageStatus => {
    const done = events.some((e) => e.event === "stage" && e.stage === stage && e.status === "done");
    if (done) return "done";
    const started = events.some(
      (e) => e.event === "stage" && e.stage === stage && e.status === "start"
    );
    return started ? "now" : "wait";
  };

  const sourceCount = events.find(
    (e): e is Extract<StageEvent, { event: "stage" }> =>
      e.event === "stage" && e.stage === "retrieve" && e.status === "done"
  )?.source_count;

  const doneCount = STAGES.filter((s) => statusOf(s.name) === "done").length;
  const fillPercent = (doneCount / STAGES.length) * 100;

  return (
    <div>
      <div className="mb-7 rounded-md bg-page px-[18px] py-3.5 text-[15.5px] leading-[1.6] text-body">
        {question}
      </div>
      <ul aria-live="polite" aria-label="Answer progress" className="m-0 list-none p-0">
        {STAGES.map((stage) => {
          const status = statusOf(stage.name);
          const label =
            stage.name === "retrieve" && status === "done"
              ? `Found ${sourceCount ?? 0} official source${sourceCount === 1 ? "" : "s"}`
              : stage.label;
          return (
            <li
              key={stage.name}
              className={
                "mb-3.5 flex items-start gap-3 text-[15.5px] leading-[1.5] " +
                (status === "done" ? "text-muted" : status === "now" ? "font-medium text-ink" : "text-[#B3A899]")
              }
            >
              <span className="mt-0.5 w-[19px] flex-shrink-0">
                {status === "done" ? I_TICK : status === "now" ? I_RUN : I_DOT}
              </span>
              <span>{label}</span>
            </li>
          );
        })}
      </ul>
      <div className="my-6 h-1 overflow-hidden rounded-full bg-rule">
        <div
          className="h-full bg-saffron transition-[width] duration-500 ease-linear motion-reduce:transition-none"
          style={{ width: `${fillPercent}%` }}
        />
      </div>
      <p className="m-0 text-[13.5px] leading-[1.65] text-muted">
        Answers are written from the sources each time, not recalled from memory. {elapsed}s
        elapsed.
      </p>
    </div>
  );
}
