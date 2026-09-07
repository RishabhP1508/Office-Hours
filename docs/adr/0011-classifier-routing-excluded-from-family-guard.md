# 0011: The Layer 2 classifier model is routable to a cheap model, and excluded from the generator/judge family guard

## Context

Phase 8 round 3 added cheap-model routing for the advice-vs-information classifier's Layer 2 escalation
(`app/guardrails/classifier.py`): `Settings.CLASSIFIER_LLM_PROVIDER`/`CLASSIFIER_LLM_MODEL`, read by
`app/providers/llm.py::get_classifier_llm` and passed into `app/pipeline.py::answer_question` as
`classifier_llm`. Empty by default (`classifier_llm=None`), which falls back to the same generator LLM
exactly as before this existed. Set, it lets Layer 2 -- a binary "is this asking for advice"
classification, not an answer a person reads -- run on a small, cheap model instead of paying the full
generator's cost and latency for a yes/no decision.

Separately, `app/providers/llm.py::assert_no_generator_judge_family_collision` (the guard behind
`ARCHITECTURE.md`'s "the eval judge is a hosted model from a different family than the generator") walks
`resolve_generator_models(settings)` -- every model in the primary+fallback generation chain -- and
checks each against `JUDGE_MODEL`'s family. `CLASSIFIER_LLM_MODEL` was deliberately left out of that
walk. This was documented in `Settings.CLASSIFIER_LLM_PROVIDER`'s own comment in `app/config.py` and in
`resolve_generator_models`'s docstring, and tested
(`services/orchestrator/tests/test_model_family_guard.py::test_classifier_model_is_excluded_from_the_family_guard`),
but never given an ADR, so the reasoning lived only in code comments a future editor could miss while
skimming the guard's call sites.

## Decision

**The classifier model is never checked by the generator/judge family guard, on purpose.** The guard
exists to stop a self-preference bias: a judge scoring text written by a model from its own family tends
to rate that text more favorably, for reasons that have nothing to do with the text's actual quality.
That bias needs a surface to act on -- text the judge actually reads and scores. The classifier's raw
output is a `{"advice": true|false}` JSON blob (`app/guardrails/classifier.py`), parsed into a boolean,
recorded on the `classify` span's `advice_decided_by` attribute, and discarded. It never becomes part of
`answer_text`, the only thing `eval/judge.py`'s judge calls (`score_comprehensibility`,
`classify_refusal`, every RAGAS metric) ever score. A judge that shares the classifier's family has
nothing of the classifier's to prefer, because it never sees anything the classifier wrote.

This means `CLASSIFIER_LLM_MODEL` can be set to anything, including a model from the same family as
`JUDGE_MODEL`, without ever tripping `assert_no_generator_judge_family_collision` -- and that is correct
behavior, not a gap in the guard.

## Tradeoff

The guard is scoped to exactly the models whose OUTPUT reaches the judge, not every model this service
happens to call. This is narrower than "check every LLM configured anywhere," but a broader guard would
reject configurations (a cheap classifier model that happens to share the judge's vendor) that pose no
actual self-preference risk, for no safety gained -- the thing the guard protects against structurally
cannot happen through a path the judge never reads.

The risk this accepts: if `app/pipeline.py` or `app/guardrails/classifier.py` is ever changed so that
the classifier's raw text (not just the parsed boolean) starts flowing into something the judge scores,
this exclusion would silently stop being safe, and nothing would catch that automatically -- the guard
checks model identity, not data flow. `test_classifier_model_is_excluded_from_the_family_guard` guards
against a future edit *widening the guard's model list* accidentally regressing this, but it cannot
guard against a future edit that changes what the classifier's output is used for. That would need a new
test asserting the classifier's raw output never reaches `answer_text`.

## Alternatives considered

- **Include the classifier model in the guard's walk.** Rejected: it would make routing the classifier
  to a cheap model from the same vendor as the judge (a reasonable, cost-saving choice) fail a check that
  is protecting against a bias this configuration cannot produce, for no actual safety benefit.
- **A separate, weaker guard for the classifier (warn instead of raise).** Rejected as unneeded
  complexity: there is nothing to warn about, since the classifier's output never reaches judged text at
  all. Adding a check with no failure mode it can ever catch is a check that exists to be read, not to
  work.
