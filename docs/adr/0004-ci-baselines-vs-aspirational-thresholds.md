# 0004: CI gates against a recorded baseline, not the aspirational thresholds

## Context

Phase 1 built `eval/run.py`: it scores a run against `THRESHOLDS`, a fixed set of quality targets
(faithfulness >= 0.85, comprehensibility >= 3.5, and so on) that the system does not meet yet. That
is the correct Phase 1 outcome, and `THRESHOLDS` is not going to move to make a number look better.

Phase 2 needs a check that runs on every pull request. `pull_request` workflows get no repository
secret and no GPU, so a `pull_request` job cannot call the hosted NVIDIA judge or the local Ollama
generator. There is no way to compute faithfulness, comprehensibility, or the judge-based refusal
classification in that job at all, and therefore no way to compare a `pull_request` run against
`THRESHOLDS` in the first place, whatever the numbers turn out to be.

Two options were on the table:

1. Gate `pull_request` against `THRESHOLDS` anyway, using whatever stand-in numbers a
   secret-free, GPU-free run could produce for the metrics it can compute.
2. Build a separate, narrower gate for `pull_request` that checks different things and is judged
   by a different standard, and keep `THRESHOLDS` exactly as the full-run target it already is.

## Decision

Two modes, two gates, never mixed.

**CI mode** (`EVAL_MODE=ci` / `python -m eval.run --ci`) runs deterministic stub providers
(`app/providers/llm.py::StubLLM`, `app/providers/embeddings.py::StubEmbedder`) over a small,
committed fixture corpus (`eval/fixtures/sources/`, 4 files chosen to exercise the same awkward
chunking paths as the real corpus: h4-only sections, the fixed-admission FAQ's inverted h2/h3
nesting, and real pre-heading content). It computes, fully programmatically, with no model call at
all: `citation_hallucination_rate`, `unreferenced_citation_rate`, `errored_rows`,
`empty_answer_rows`, every subset breakdown, `reading_grade_level` (textstat, local), and
`false_refusal_rate`/`advice_leakage_rate` using a regex-based approximation of the judge's
classification (`eval/run.py::CI_REFUSAL_PATTERNS`). It skips `comprehensibility`, `faithfulness`,
`answer_relevancy`, and `context_precision` entirely and prints them as `SKIPPED (CI mode)`. CI
mode's gate is `eval/baselines.json`'s `ci_baseline`: the current run must be no worse than the
last CI-mode run a human accepted, within a small float tolerance (`eval/run.py::CI_BASELINE_GATE`).
Not "meets `THRESHOLDS`" -- CI mode's numbers have no relationship to `THRESHOLDS` at all, since
there is no judge, no RAGAS, and the corpus is not the real one.

**Full mode** (the default, unchanged from Phase 1) runs against the real providers and the real
corpus, judged by the hosted NVIDIA endpoint and scored by RAGAS. It still gates on `THRESHOLDS`,
exactly as before, and now additionally prints a comparison against `eval/baselines.json`'s
`full_run_reference` (the Phase 1 run of record) purely for tracking the gap over time; that
comparison is informational only and never changes a full run's exit code.

`THRESHOLDS` itself is never touched by any of this. It stays the fixed Phase 4 aspirational target,
exactly as CLAUDE.md and ARCHITECTURE.md require, and a full local run is still the only thing
measured against it.

## Why

A gate that always fails gates nothing: if `pull_request` were held to `THRESHOLDS`, and the system
is known not to meet several of them yet (Phase 1's own run of record fails four), the check would
be red on every single pull request from day one, for a reason that has nothing to do with the
change in that pull request. A gate the user routinely overrides to merge anyway is a gate they
learn to ignore, and an ignored gate stops catching real regressions. The only way to make the
`pull_request` check meaningful is to make it check something the change in front of it could
plausibly have broken -- retrieval, citation mapping, refusal bookkeeping, error handling -- and to
hold it to a standard that can actually move with deliberate improvement: "no worse than last time,"
not "as good as a target nothing has hit yet."

Keeping `THRESHOLDS` and the CI baseline as two separate files with two separate purposes is also
what stops this from becoming the "loosen it until it passes" anti-pattern CLAUDE.md forbids
outright. Nothing about building CI mode ever required editing a single value in `THRESHOLDS`; it
required building a second, honestly-scoped gate next to it.

## Tradeoff

The `pull_request` check genuinely does not measure answer quality, ever. A pull request could
regress `faithfulness` or `comprehensibility` badly and this check would stay green, because it
never runs the judge or RAGAS at all. The banner CI mode prints exists specifically to stop that
from being mistaken for a passing quality eval: "No LLM judge ran. No RAGAS metric ran... A green
result here is NOT a passing quality eval." The real quality signal only ever comes from a full run
(locally, or the `workflow_dispatch` job with `secrets.JUDGE_API_KEY`), and that has to be triggered
deliberately -- there is no way around a human deciding when to spend the judge/RAGAS budget and
look at the real numbers.

The other real cost: the CI baseline can go stale. If a future change legitimately makes
`false_refusal_rate` worse in CI mode for a good reason (say, the guardrail classifier lands in
Phase 4 and changes StubLLM's refusal behavior in some direction that is correct but different),
someone has to consciously re-record `eval/baselines.json`'s `ci_baseline` from a new accepted CI
run, the same way this phase did. That is a deliberate, visible action (a file diff a reviewer sees
and can question), not an automatic ratchet in either direction, and it is the price of the baseline
being meaningful at all.

## Superseded in Phase 4 step 3 (2026-09-05): the two refusal rates are now gated in CI mode

Everything above this section is left exactly as written, because it was correct for what existed
at the time: through Phase 4 step 2, CI mode's `false_refusal_rate` and `advice_leakage_rate` were
computed from a regex-based approximation of the judge (`eval/run.py::CI_REFUSAL_PATTERNS`, since
removed) over `StubLLM`'s own hardcoded advice-detection patterns
(`app/providers/llm.py::_STUB_ADVICE_PATTERNS`, also since removed). Gating either rate would have
measured whether that stub-only heuristic agreed with `eval/golden.jsonl`'s `is_advice` label, not
whether the real system refuses correctly -- exactly the tautology this ADR, and
`eval/run.py::CI_BASELINE_GATE`'s original comment, argued against. This ADR's own Tradeoff section
above anticipated the fix in advance almost exactly: "If a future change legitimately makes
`false_refusal_rate` worse in CI mode for a good reason (say, the guardrail classifier lands in
Phase 4 and changes StubLLM's refusal behavior in some direction that is correct but different),
someone has to consciously re-record `eval/baselines.json`'s `ci_baseline`." That is precisely what
happened.

What changed: the advice-vs-information decision moved out of `StubLLM` entirely and into
`app/guardrails/classifier.py`'s rule layer (Layer 1) -- real production code, the same code path a
real request takes, not a stub-only heuristic built only to give CI something to detect. CI mode now
reads each response's structured `response_type` field (`app/schemas.py::ResponseType`) through
`eval/run.py::classify_refusal_structured`, replacing the deleted `CI_REFUSAL_PATTERNS` regex
entirely. There is no longer a stub-specific decision to game by tuning a pattern list, because there
is no longer a pattern list in the classification path at all -- `ADVICE_PATTERNS` in
`app/guardrails/classifier.py` is real, production-facing logic, checked (in
`services/orchestrator/tests/test_guardrails.py`) against the same "never match exactly one golden
row" invariant this ADR's underlying concern was always about. With the tautology gone,
`false_refusal_rate` and `advice_leakage_rate` moved into `eval/run.py::CI_BASELINE_GATE`
(`higher_is_better=False`, `tolerance=0`, since CI mode is fully deterministic under stub providers),
gated exactly like `citation_hallucination_rate` already was. `CI_REFUSAL_BOOKKEEPING_GATE` is
unchanged and still gates alongside them, not in place of them.

One limitation is unchanged and worth restating here: `LLM_PROVIDER=stub` means Layer 2 (the model
escalation in `classify_advice`) never runs in CI mode, so any golden row whose advice phrasing only
Layer 2 would catch reads as a leaked advice row in this gate. That is an honest measurement of the
rule layer's coverage on its own corpus-independent code path, not a gap in this gate's integrity --
see `eval/run.py`'s module docstring for the exact rows this affects and why adding a pattern to
"fix" it would recreate the tautology this section describes moving away from.

## Alternatives considered

- **A second, weaker set of thresholds for CI mode.** Rejected: this is the same "loosen it until it
  passes" pattern with a different name. A weaker threshold is still a threshold someone tuned to
  make a check pass, and it invites being tuned again the next time it is inconvenient.
- **Skip the `pull_request` check on quality metrics but keep the same THRESHOLDS keys, marked
  `n/a`.** Rejected as a gate: with nothing computed, there is nothing to compare and nothing to
  fail on, so this is not a check, it is documentation. CI mode does exactly this for the four
  skipped metrics, but the gate that actually decides pass/fail is the baseline comparison on the
  metrics that ARE computed, not a vacuous pass on the ones that are not.
- **Give the `pull_request` job GPU access and a judge key so it can run the real eval.** Rejected:
  `pull_request` workflows run for anyone who opens a PR, including from a fork, and GitHub does not
  grant repository secrets to fork-triggered `pull_request` runs by design (a fork could otherwise
  exfiltrate the secret). Handing a judge API key to every PR run would also mean paying for a judge
  call on every commit pushed to every open PR, for a signal (answer quality) that a single commit
  rarely moves.
- **One eval script mode, with the caller supplying a corpus and provider config.** Considered and
  partially adopted: `INGEST_MODE=snapshot` (`app/ingest.py`) is exactly this idea applied to
  ingestion, so the fixture corpus reuses the same chunker and storage code as the real one instead
  of a parallel path. The eval script itself still needed a real mode split, because which metrics
  can be computed at all (not just which corpus/providers are used) differs between the two cases.
