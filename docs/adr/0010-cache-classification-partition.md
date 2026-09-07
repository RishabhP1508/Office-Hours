# 0010: The semantic cache partitions on the advice/information classification, not just corpus version

## Context

Phase 8 round 3 added a semantic response cache (`app/cache.py`): a repeat question, close enough by
cosine distance to a prior question's embedding, is served that prior question's already-verified
`AnswerResponse` instead of paying for another retrieval/generation/verification cycle.
`SEMANTIC_CACHE_SIMILARITY_THRESHOLD` (0.15) was derived from real measurements against
`eval/golden.jsonl`'s 21 questions: paraphrases of the same question landing as close as 0.0394, and
the closest genuinely-different pair among those 21 landing at 0.1595. The round-3 report treated that
0.1595 figure as the threshold's binding constraint.

Round 4 review measured a different, more dangerous pool: real factual/advice pairs on the SAME
topic, phrased the way an actual user would phrase them, rather than pairs drawn from a golden set
deliberately written to be distinct from itself. Using the SAME embedder (nomic-embed-text) this
project's query path already uses, two of six such pairs measured landed INSIDE the 0.15 threshold:

- "Does my employer need E-Verify for the STEM extension?" (informational) vs. "Should I switch to an
  E-Verify employer to get the STEM extension?" (advice-seeking): **0.0885**.
- "Which form do I file for the OPT work permit?" vs. "Which form should I file for my OPT work
  permit?": **0.0175**.
- (0.1563, 0.1664, 0.2194, 0.2430 for the other three measured pairs.)

Reproduced end to end against the real pipeline, with the real 221-chunk corpus, same question asked
twice, only `SEMANTIC_CACHE_ENABLED` differing:

    cache OFF -> response_type=refusal_advice  refusal_reason=query_asks_for_personal_advice  DSO redirect present   1 model call
    cache ON  -> response_type=answer          refusal_reason=None                            DSO redirect ABSENT    0 model calls

`app/pipeline.py` had already computed the correct classification (`classification.is_advice`) before
ever reaching the cache lookup; `cache.lookup()` simply never looked at it, so the classifier's answer
was thrown away on this path. Nothing about the classifier itself was wrong.

## Decision

**Partition `lookup()` on the classification, not on a distance threshold.** `cache.lookup()` now
takes `is_advice: bool` as a REQUIRED, keyword-only argument (no default) and only ever considers a
cache row whose stored `response_type` corresponds to that same classification (`ANSWER` for
`is_advice=False`, `REFUSAL_ADVICE` for `is_advice=True`). This needs no new column: `app/pipeline.py`
already derives `response_type` from `classification.is_advice` with no other path
(`REFUSAL_ADVICE if classification.is_advice else ANSWER`), so `response_type`, already persisted by
`store()`, already IS the classification. `app/pipeline.py`'s call site now passes
`is_advice=classification.is_advice` -- the value it had already computed and was previously
discarding.

**The real lesson: a similarity threshold cannot separate intent from topic.** An embedding encodes
what a question is ABOUT far more strongly than whether it is asking for a fact or asking for advice
about that same fact. "What is the rule on X" and "what should I do about X" are neighbours in
embedding space by construction, not by an unlucky choice of threshold. Lowering
`SEMANTIC_CACHE_SIMILARITY_THRESHOLD` until the two measured pairs above stop colliding would tune this
project specifically against the six pairs the round-4 review happened to send, and the next real
user's phrasing would collide again for the identical structural reason. There is no number that
fixes this, because the axis a distance measures (topic) is not the axis the safety boundary needs
(intent). The fix has to make the safety-relevant fact -- which classification produced this cached
response -- part of what `lookup()` is allowed to match on, rather than something a distance is
trusted to preserve.

**Why `is_advice` is required, not optional-with-a-default.** A default of "match any classification"
would silently reopen this exact bug the next time a call site is added or refactored. Requiring it
means a future call site that forgets to pass it fails to type-check/call at all, not fails silently
at 2am against a real user's advice-seeking question.

## Residual risk, stated honestly

This partition closes the cross-classification failure above. It does **not**, and cannot, close every
semantic-cache failure mode: two genuinely different questions that share both a topic AND the same
classification (two distinct purely-informational questions about the same form, say) can still land
within `SEMANTIC_CACHE_SIMILARITY_THRESHOLD` of each other and collide, and the wrong one's
fully-verified, real-looking citations would be served for the other. That risk is inherent to any
similarity-based cache and is not eliminated here. What this decision removes is specifically the risk
of crossing the advice/information line, which CLAUDE.md and ARCHITECTURE.md treat as a hard safety
boundary, not a quality nuance -- narrower than "the cache is never wrong," but exactly the boundary
that must never be crossed.

## Tests

`services/orchestrator/tests/test_cache.py` adds:

- `test_lookup_never_crosses_the_advice_information_boundary` (parametrized over both directions:
  factual-stored/advice-queried and advice-stored/factual-queried), using hand-picked vectors placed
  well inside the threshold -- the whole-class guard, independent of any real embedder, that fails on
  ANY classification mismatch, not just the one pair the bug report described.
- `test_lookup_still_hits_when_the_classification_matches`, proving the partition does not simply
  break caching for the matching case.
- `test_cache_never_serves_an_advice_answer_cached_for_a_purely_factual_question`, the real-world
  reproduction: the exact "E-Verify" pair above, embedded with the real nomic-embed-text embedder
  (measured distance re-asserted `< 0.15` at test time so the test fails loudly if a future embedder
  change moves that pair outside the threshold instead of passing for the wrong reason), driven through
  the real `answer_question` pipeline end to end. Skipped only when no reachable Ollama is present
  (matching this project's existing `full_corpus`-style convention for tests that need real
  embeddings), never weakened to run against a meaningless stub embedder.

## Alternatives considered

- **Lower the threshold.** Rejected -- see "The real lesson" above: no threshold separates these two
  pools, since they overlap by construction (0.0175/0.0885 advice-vs-factual pairs sit well inside the
  0.0394 same-intent-paraphrase floor already measured in round 3).
- **Disable the cache for any question Layer 1's rule-based classifier does not confidently match.**
  Rejected: this narrows exposure but does not close it -- Layer 2 (the model escalation) and even
  Layer 1 can and do classify correctly on questions this heuristic would call "unconfident," and
  disabling caching for that whole set throws away real cache value for no safety gain the
  classification-partition does not already provide for free.
- **A separate boolean column instead of reusing `response_type`.** Rejected as unnecessary: it would
  duplicate a fact `response_type` already carries 1:1, with no additional test coverage or clarity
  gained, and a second column can drift from the first in a way a single derived value cannot.
