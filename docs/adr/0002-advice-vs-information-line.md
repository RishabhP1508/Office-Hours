# 0002: Where the advice line falls, and why a rule decides before a model does

## Context

ARCHITECTURE.md fixes the product boundary before this phase: the system says what the rule is and
where it is written; it does not say what a person should do, whether their filing will be approved,
or which status or path suits them. Phase 4 has to turn that boundary into running code -- something
that looks at a question and decides, for that one question, which side of the line it falls on --
and has to do it in a way that runs everywhere this project runs, including the `pull_request` CI job
that has no GPU, no hosted API key, and no live model to call at all
(docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md).

A second question sits right next to the first: once a question is classified as advice-seeking,
what does the system actually do with it? The golden set's advice rows (eval/golden.jsonl) are not
answered with a canned refusal. Row 4's ground truth, for the OPT-timing question, is: "I can't tell
you which to choose, since that depends on your own situation. What I can tell you is the rule: any
pre-completion OPT you use gets deducted from your post-completion OPT, so using time before
graduating leaves you less afterward. For advice on your specific case, talk to your DSO or a
licensed immigration attorney." That is a grounded, cited statement of the rule, immediately followed
by a decline to choose for the reader. A template that skips retrieval and returns a fixed refusal
string cannot produce that, and would be scored badly -- correctly -- on faithfulness and
comprehensibility for not even trying.

## Decision

**Where the line falls.** The system states the rule and cites where it is written. It never tells
the reader which option to pick, whether their filing will be approved, or what their odds are. That
boundary is enforced twice: once by a classifier that decides which system prompt to use, and once by
the system prompt itself, which is never allowed to give advice regardless of which prompt was
picked.

**Two layers, rule before model.** `app/guardrails/classifier.py::classify_advice` tries Layer 1
first: a deterministic, high-precision substring match over generic English advice-seeking phrasings
("should I", "which is better", ...), checked only against the question text. Layer 1 runs
everywhere, including the `pull_request` CI job, because it needs no network call and no model. Layer
2 -- one call to the configured LLM asking for strict JSON `{"advice": true|false}` -- runs only when
Layer 1 found nothing AND the configured provider is not the stub. Layer 2 can only turn an
information verdict into advice; it can never overturn a Layer-1 advice verdict back to information,
and a failed, timed-out, or unparseable call falls back to "information" rather than raising, so a
classifier outage never takes `/query` down.

The rule layer goes first, not the model, because ARCHITECTURE.md's stated preference is a mechanical
check over a model's opinion wherever one is possible, and because only the rule layer can run in CI
at all -- if the model layer decided first, the CI invariant gate would have nothing of the real
classification logic to test.

**An advice verdict still retrieves.** `app/pipeline.py` runs retrieval identically regardless of the
classification, then generates with `REFUSAL_SYSTEM_PROMPT` instead of `SYSTEM_PROMPT` (both in
`app/prompts.py`) for an advice verdict. `REFUSAL_SYSTEM_PROMPT` states the general rule from the same
retrieved context, with the same bracket citations, and additionally forbids choosing an option,
predicting an outcome, or estimating odds, ending with a directive to the reader's DSO or a licensed
attorney. `app/pipeline.py` appends that DSO/attorney sentence programmatically if the model's own
output does not already contain one (checked with a small regex,`_has_redirect`, so it is never
appended twice), so the redirect is guaranteed present even if the model forgets it. This is the only
design that can reproduce row 4's shape of answer: state the rule, cite it, then decline to choose.

**REFUSAL_ADVICE and NO_ANSWER are separate `ResponseType` values.** ARCHITECTURE.md treats "the
question asks for advice" and "the sources don't cover this" as two different failure modes, each
with its own test. Collapsing both into one generic "refused" value would throw that distinction away
at exactly the layer (the API response shape) where eval/run.py and anything else consuming the
response needs it intact.

## Tradeoff

A high-precision rule layer, by construction, misses advice phrasings nobody wrote a pattern for.
Two golden rows make this concrete: "What are my odds in the H-1B lottery this year?" and "Should my
employer put me in at a higher wage level so I have a better shot?" match none of
`ADVICE_PATTERNS` (both patterns that would have matched them, "my odds" and "should my", were removed
earlier for a different reason -- each matched exactly one golden row and made that row's
classification a tautology rather than a measurement). Under the stub provider, where Layer 2 never
runs, both would be misclassified as informational. Two things stand between a miss like this and an
actual advice-shaped answer reaching a real user: Layer 2 (in any environment with a real LLM
configured), and `SYSTEM_PROMPT`'s own rule 5 ("never give advice"), which was deliberately never
retired when the guardrails landed (see `app/prompts.py`'s module docstring). Rule 5 is not
decorative -- it is the only thing left standing on a question no classifier layer caught. This is
also why CI mode's `false_refusal_rate`/`advice_leakage_rate` (gated as of Phase 4 step 3 against a
recorded baseline, see `eval/run.py::CI_BASELINE_GATE` and
`docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md`'s "Superseded" section) measure Layer 1's
coverage alone under `LLM_PROVIDER=stub`, not the full system a real user talks to: Layer 2 never
runs there, so a miss like this one reads as a leaked advice row in CI's own numbers.

## Alternatives considered

- **A model-only classifier**, skipping the rule layer entirely. Rejected for this phase: a
  `pull_request` CI job gets no GPU and no hosted API key
  (docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md), so a model-only classifier would run in
  no environment CI can reach, and the CI refusal gate would be checking nothing at all about the
  actual classification logic -- only whichever stub behavior stood in for it.
- **A canned refusal template for any advice verdict, no retrieval.** Rejected: it cannot produce the
  golden set's actual expected shape of answer (state the rule, cite it, decline to choose), which
  costs real faithfulness and comprehensibility score for the wrong reason -- not because the system
  gave bad information, but because it gave none.
- **One `ResponseType` value for every kind of refusal.** Rejected: it would erase the distinction
  ARCHITECTURE.md draws between refusing advice and not knowing, at the one layer (the response shape
  itself) where a consumer of that field could otherwise tell them apart mechanically instead of
  re-deriving the distinction from the answer's prose.
