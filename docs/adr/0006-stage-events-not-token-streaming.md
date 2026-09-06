# 0006: Stage events over SSE, not token streaming

Status: accepted (Phase 6)

## Context

A query takes several seconds against the local generator, and the Phase 6 brief budgeted for 20 to
140 seconds. A spinner held that long reads as a broken page. The audience is people checking rules
about their own immigration status, often on a phone, and they will assume a site that hangs is fake
and leave. So the interface has to say something true while it waits.

The obvious answer is to stream the generator's tokens as they arrive, the way most chat interfaces
do. Ollama supports it, and it would put words on screen in about a second.

We cannot do it, and the reason is structural rather than a matter of effort.

`app/pipeline.py` runs citation verification after generation, not during it. Step 7 calls
`verify_citations`, and when a generated answer cites an index that was never retrieved, or an
ANSWER carries no citation at all, the pipeline throws the generated text away and returns
`BLOCKED_UNVERIFIED` with a fixed safe message instead. CLAUDE.md states the rule this implements:
an answer with any claim that does not map to a retrieved chunk must be blocked before it renders.

Token streaming renders text as it is produced, which is necessarily before verification has run.
An answer that fails the citation check would already be on the reader's screen, and the only
remaining move would be to retract text they had started reading. On a tool about immigration
status, showing an uncited claim and then withdrawing it is worse than showing nothing for the same
number of seconds. Verification would become advisory, which is exactly what it exists not to be.

Streaming only after verification would mean buffering the whole answer first, which delivers no
token-level benefit at all.

## Decision

Do not stream the answer body. Stream progress through the pipeline stages instead.

`POST /query/stream` emits Server-Sent Events over an `asyncio.Queue`. `answer_question` takes an
optional `on_event` callback and awaits it at the four stage boundaries it already had: classify,
retrieve, generate, verify. The retrieve event carries the real number of chunks retrieved. The
terminal message carries the complete, already verified `AnswerResponse`, the same object
`POST /query` returns.

Events fire only for stages that actually ran. A vague query returns after classify and emits
nothing for retrieve, generate, or verify, because none of them executed.

`on_event` defaults to `None`, and `POST /query` passes nothing, so the existing endpoint's
behaviour and response shape are unchanged. `eval/run.py` reads that endpoint and its Phase-to-Phase
comparisons depend on it not moving.

The progress bar advances on received stage events only. It is not driven by a timer. The waiting
view also shows an elapsed-seconds counter, which is a measured quantity rather than an estimate of
remaining work.

## Tradeoff

The reader waits for the whole answer rather than watching it appear. Measured over eight real
queries against the warm local generator, the first server byte arrives in 5 ms at p50, the real
"Found 5 official sources" line at 0.82 s, and the full verified answer at 5.5 s p50 and 10.0 s p95.
The first informative render happens at 2.7 ms, because the stage list and the echoed question are
drawn from client state before the network call starts.

What we give up is the perception of speed that token streaming buys. What we keep is that nothing
reaches the screen until the citation check has passed on the exact string that will render.

## Alternatives considered

**Stream tokens and retract on a failed check.** Rejected. It puts unverified claims in front of the
reader and makes the guardrail advisory.

**Stream tokens only for responses that pass.** Not possible. Whether a response passes is not known
until generation has finished.

**Verify incrementally, sentence by sentence, as tokens arrive.** Rejected for this phase. Bracket
citations can appear anywhere in a sentence and the index range is only meaningful against the full
retrieved set, so partial verification would either block valid prose or pass text that a later
token invalidates. It also replaces one mechanical check with a much larger amount of machinery, for
a latency win that is small now that measured p50 is 5.5 s.

**An indeterminate spinner with an estimated progress bar.** Rejected. The Phase 6 brief rules out a
bar that is not tracking anything, and an invented estimate is a small lie told to an audience whose
reason for being here is that they need to trust what the page says.

**A Next.js route handler proxying the stream.** Rejected. It adds a hop and a file the repository
layout does not carry, and Phase 7 puts a Go gateway in exactly that position. The browser calls the
orchestrator directly, and the orchestrator answers the preflight itself using `ALLOWED_ORIGINS`.
