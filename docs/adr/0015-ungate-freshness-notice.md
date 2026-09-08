# 0015: Ungate the freshness notice; annotate dated passages for the model instead

## Context

Red-team verification on 2026-09-07 reproduced a live defect, eight days before the DHS
fixed-period-of-admission final rule takes effect (2026-09-15) and cuts the F-1 post-completion
departure period from 60 days to 30. Six real phrasings of "how long do I have to leave the US
after my program ends" were run six times each against the live production site. On 5 of 6 runs of
the plainest phrasing, three fixed-admission chunks were retrieved (ranks 3-5) and listed in the
answer's own source list, but `freshness.notices` came back empty and the rendered answer stated a
bare "60 days" with no effective-date qualification at all. Two other phrasings ("grace period if I
transfer to a new school", "grace period when I change education level") stated the future 30-day
rule as though it applied today, with no date. A fourth ("H-1B petition denied during cap-gap")
retrieved no dated source at all.

Two separate mechanisms produced this:

1. `app/guardrails/freshness.py::build_freshness` only emitted a `FreshnessNotice` for a dated
   source that was either the top-ranked retrieved chunk or actually cited in the generated text.
   That gate existed for a real reason (see below), but it fired on the majority of real runs of
   the same real question, suppressing the one signal that most needed to reach the reader.
2. `app/prompts.py::format_context` rendered every passage identically -- `[N] Source: <url>` plus
   content -- whether or not it carried a `rule_effective_date`. The prompt told the model to state
   both a current rule and a dated replacement when the context held both, but gave it no way to
   tell which passage was which except by reading and understanding unmarked prose. Measured
   against the six phrasings, that worked roughly 1 run in 6.

## Decision

Three changes, all reversible independently, none touching `eval/golden.jsonl`, the eval
thresholds, or the judge:

**1. Annotate the passage, not just the answer.** `app/prompts.py::format_context` now takes
`today` and appends a one-line, mechanically-generated note ("this passage describes a rule that
takes effect on `<date>`" / "took effect on `<date>`") to any passage whose chunk carries a
`rule_effective_date`. The wording is derived only from the date and whether it has passed relative
to `today` -- never a hardcoded description of what changed -- so it stays correct for the next
dated rule the corpus picks up, not just this one. A passage with no `rule_effective_date` renders
byte-for-byte as it did before.

**2. Sharpen the prompt rule to bind to that annotation explicitly.** Rule 4 (`SYSTEM_PROMPT`) and
rule 1 (`REFUSAL_SYSTEM_PROMPT`) now name the annotation directly: when some passages carry a
future "takes effect on" note and others do not (or carry a "took effect on" note), the model must
state the current rule with the date it applies until, and the replacement rule with the date it
takes effect, and never state a single fact as though only one rule applied at all times.
`REFUSAL_SYSTEM_PROMPT` got the same fix, not just `SYSTEM_PROMPT`, because two of the six
red-team phrasings routed to the advice-refusal path and had exactly the same defect there.

**3. Remove the notice gate.** `build_freshness` now emits a `FreshnessNotice` for every distinct
retrieved source that carries a `rule_effective_date`, regardless of rank or citation. The
`app/guardrails/freshness.py` module docstring records the original gating reasoning in full,
labeled SUPERSEDED, rather than deleting it, alongside the red-team evidence that overrode it.

## Tradeoff and alternatives considered

**Keep the gate, fix only the prompt.** Rejected. The gate was the more direct cause of the 5-of-6
failure: a model that correctly infers a passage is dated from the new annotation still produces no
visible notice at all if that passage is neither top-ranked nor cited, which is exactly the rank the
real fixed-admission chunks sat at for the departure-period question (3-5, per the bug report).

**Widen `FreshnessNotice.reason` to a third value for "retrieved only, neither top-ranked nor
cited."** Round 1 (2026-09-07) did NOT do this: `app/schemas.py` was out of that round's scope by
design (the task deliberately drew the boundary at the freshness guardrail, the prompt, and the
pipeline glue, not the response schema), so a notice whose source was genuinely neither top-ranked
nor cited reported `reason="cited"` anyway -- a documented, honest KNOWN IMPRECISION rather than a
silently accepted one. Round 2 (2026-09-08), after independent verification caught this exact case
(a rank-5 source with `cited_indices={1,2}` reporting `reason="cited"`), added the third value,
`"retrieved"`, to `app/schemas.py::FreshnessNotice.reason`'s `Literal` and to
`services/frontend/lib/api.ts`'s matching union type, and restored the `cited_indices` check in
`build_freshness` that round 1 had stopped reading. Every value `reason` can carry is now an
accurate statement about the source it describes; nothing about whether a notice fires changed.

**The noise cost, measured rather than assumed.** Removing the gate means a dated notice can now
fire on a question that only incidentally retrieves a fixed-admission chunk -- the exact case the
gate was built to avoid. Measured directly against all 21 `eval/golden.jsonl` questions, run
end-to-end through the live local stack (qwen3.5-8k generator) before and after this fix: 1 of 21
carried a notice before ("How long is post-completion OPT?", which genuinely does border the
departure-period FAQ), rising to 3 of 21 after -- the two new ones are "What is the I-983 and who
fills it out?" and a pre-completion-OPT day-counting question, neither of which is about the
departure-period rule at all. Both are real instances of the noise the original gate existed to
prevent. The number is reported, not tuned toward: 2 additional spurious notices out of 21 real
questions is the honest cost of this fix, and it stands anyway, because a spurious-but-generic date
notice is a materially smaller harm than a silently wrong "60 days" stated as though it were
settled.

**Measured on the local model, not assumed to transfer.** The local dev stack generates with
`qwen3.5-8k` via Ollama; production generates with `gpt-oss:120b` via Ollama Cloud. The red-team
report that motivated this fix was captured against production. Verification of this fix was run
against the local stack because that is the environment available to the builder session; the
6x5 consistency table measured here is reported as a local-model number, explicitly not implied to
be the production rate. Re-verifying against the production provider before relying on this number
for a launch decision is a real, open follow-up, not covered by this ADR.

**Honest result on qwen3.5-8k, stated plainly rather than smoothed over:** the BOTH/CURRENT_ONLY/
FUTURE_ONLY/NEITHER distribution over the 6x5 table did not meaningfully shift between before and
after this fix (roughly the same mix of all four buckets both times; see the phase report this ADR
ships alongside for the full table). All three changes work as designed and are unit-tested in
isolation -- the annotation reaches the model (confirmed directly in the rendered prompt), the
notice fires unconditionally on retrieval alone, and the trailing sentence always names the date
when any dated source is retrieved. What did NOT reliably change is whether qwen3.5-8k's own prose
states both numbers when asked "what is the grace period after OPT ends" or "H-1B cap-gap denial" --
for the latter, retrieval never surfaces a dated chunk at all on this local corpus, which is a
retrieval-recall gap this fix does not address and was never scoped to. This is reported rather than
hidden or re-tuned against, per the task's own anti-gaming boundary: a generic fix that measurably
improves the structured signal (the notice) without measurably moving a small, noisy 30-sample
prose-classification rate on one specific local model is still the right fix, but the local
measurement should not be read as proof the defect is closed end to end.

## The general lesson

A prompt rule that describes a fact the model has to infer from unmarked prose is a rule the model
will follow inconsistently, in proportion to how hard that inference is. Making the fact explicit in
the input (an annotation, not a hope) turns "does the model realize this passage is dated" from a
question about model capability into a question about whether the pipeline handed the model the
fact at all. The freshness notice's gate was a reasonable answer to a real noise problem, but it was
evaluated only against the case it was designed to prevent (an incidental hit on an unrelated
question), never against the case where the reader most needed the signal it was suppressing. Both
costs have to be measured before a gate on a safety-relevant signal is trusted to be a net
improvement, not just the one the gate was drawn to prevent.
