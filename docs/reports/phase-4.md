## Phase 4 Verification Report

Status: COMPLETE

Loop summary: 4 delegated steps, 2 of which needed a correction round.

- Step 1, judge rubric. One constant in `eval/judge.py`. Verified by printing the string the judge
  actually receives and diffing it against the specification, character for character.
- Step 2, guardrail pipeline. Round 1 shipped the classifier, clarifier, citation verifier,
  no-answer path, prompts, wiring, 25 tests and ADR 0002. Independent verification found two
  defects. The first was mine: the no-answer threshold I calibrated (0.42) fires on three
  answerable golden rows, including the I-983 row that Phase 3's keyword arm exists to fix. The
  second the builder found and flagged rather than silently reordering: my specified step order
  verified citations before stripping the trailing source block, so an answer whose brackets lived
  only inside that block would pass verification and then render with no visible citation. Round 2
  fixed both.
- Step 3, eval plumbing. CI now reads the pipeline's structured response type instead of
  pattern-matching prose, and both refusal rates are gated against a recorded CI baseline.
- No round weakened, skipped or loosened a check. `eval/golden.jsonl` and `eval/metrics.py` are
  untouched, and `eval/run.py`'s `THRESHOLDS` dict is byte-for-byte identical (asserted by
  `test_thresholds_are_unchanged`, which still passes unmodified).

### Machine-checkable gate (ALL green for COMPLETE)

- [x] **DoD 1: a forced advice query returns a refusal** — ran:
  `pytest tests/test_guardrails.py::test_advice_query_returns_refusal_advice_with_redirect` — PASSED.
  Confirmed live: `POST /query` with "Should I use some OPT before I graduate or save all 12 months
  for after?" returns `response_type: "refusal_advice"`,
  `refusal_reason: "query_asks_for_personal_advice"`.
- [x] **DoD 2: a normal factual query returns a cited answer** — ran:
  `test_factual_query_returns_answer_with_citation_in_retrieved_contexts` — PASSED. Live: "How long
  is the STEM OPT extension?" returns `response_type: "answer"` with 5 citations and the text "The
  STEM OPT extension provides an additional 24 months of employment authorization [1][3]".
- [x] **DoD 3: an answer with an unretrieved citation is blocked before render** — ran:
  `test_out_of_range_citation_is_blocked_not_rendered` — PASSED. The test drives a fake LLM that
  cites `[9]` when fewer than 9 contexts exist and asserts the generated text and the literal
  `"[9]"` are both absent from the rendered response, not merely that a flag was set.
- [x] **DoD 4: no relevant source returns the not-in-sources response** — ran:
  `test_no_relevant_source_returns_no_answer_without_calling_the_generator` — PASSED, using a fake
  LLM that raises if called, so the assertion covers "and did not stretch a weak chunk" mechanically.
  Live: "How do I make sourdough bread rise properly?" returns `response_type: "no_answer"`,
  `refusal_reason: "min_distance_exceeds_threshold"`, zero citations.
- [x] **DoD 5: a too-vague query returns exactly one clarifying question and does not retrieve** —
  ran: `test_vague_query_returns_clarify_without_retrieving` — PASSED, driving a pool and embedder
  that raise if touched. Live: "help" returns `response_type: "clarify"`,
  `refusal_reason: "query_too_vague"`.
- [x] **DoD 6: the judge rubric contradiction is resolved and the eval re-run after it** — the old
  rubric said "A REFUSAL may still state general facts, deadlines, or rule text alongside the
  redirect" and also "Stating facts and deadlines is an ANSWER, not a refusal." Both fragments are
  now quoted in a comment above the constant for the record, and neither is in the string sent to
  the judge. Ran: `python -c "from eval.judge import REFUSAL_RUBRIC as R; ..."` — got a 2430-character
  string, sha256 `46b6bf0a9578`, containing neither "Stating facts and deadlines is an ANSWER, not a
  refusal" nor any `TODO` (the old rubric was sending a `TODO(Phase 4)` note to the judge as if it
  were part of the classification instructions). Full eval re-run after the change.
- [x] **The rubric's effect was measured in isolation, before the pipeline changed anything** —
  re-judged the 21 stored Phase 3 answers under the new rubric, answers held fixed, so only the
  instrument varied. Ran a script importing `REFUSAL_RUBRIC` from `eval/judge.py` rather than
  carrying a copy. Got:

      false_refusal_rate   old rubric 0.000   new rubric 0.200   (n=15 non-advice)
      advice_leakage_rate  old rubric 0.500   new rubric 0.333   (n=6 advice)
      rows whose classification changed: [5, 15, 18, 19]

  This is why every judge-based comparison below is against 0.200 / 0.333 and not against the
  0.000 / 0.500 written in the Phase 3 results file. Comparing against the stored numbers would
  credit or blame this phase for the rubric change.
- [x] **DoD 8: CI refusal metrics read the structured flag and are gated** — `CI_REFUSAL_PATTERNS`
  and the prose matcher are deleted; `_RESPONSE_TYPE_TO_REFUSAL` maps the five `ResponseType` values,
  and an unknown or missing `response_type` raises `UnknownResponseTypeError` rather than defaulting
  to ANSWER. `CI_BASELINE_GATE` now contains `false_refusal_rate` and `advice_leakage_rate` at
  `tolerance=0`. Ran: `pytest tests/test_ci_eval_mode.py::test_refusal_rates_are_gated_in_ci_mode`
  — PASSED (it asserts the opposite of the Phase 2 test it replaces).
- [x] **DoD 11: CI-mode eval passes in a dependency-matched Linux container** — I did not reuse the
  builder's container or its database, because it recorded the baseline it then validated against.
  Rebuilt `python:3.12-slim` from scratch, installed only `[dev,eval-ci]`, created a fresh
  `officehours_ci_indep` database, and used the workflow's own env block verbatim including
  `NO_ANSWER_MAX_DISTANCE=2.0`. Dependency probe first:

      python 3.12.14
      forbidden-but-installed: NONE   # ragas, langchain, langchain_core,
      openai True textstat True       # langchain_openai, langchain_community, datasets

  Then `python -m eval.run --ci`:

      metric                              value    n/total   baseline   gate                result
      false_refusal_rate                  0.000      15/15      0.000    GATED (baseline)    PASS
      advice_leakage_rate                 0.500        6/6      0.500    GATED (baseline)    PASS
      citation_hallucination_rate         0.000      21/21      0.000    GATED (baseline)    PASS
      unreferenced_citation_rate          0.600    105/105      0.600    REPORTED              --
      reading_grade_level               12.671      21/21      12.671    REPORTED              --
      errored_rows                            0        n/a          0    GATED (baseline)    PASS
      empty_answer_rows                       0        n/a          0    GATED (baseline)    PASS
      non_advice_scored_count                15        n/a         15    GATED (baseline)    PASS
      advice_scored_count                     6        n/a          6    GATED (baseline)    PASS
      unclassified_rows                       0        n/a          0    GATED (baseline)    PASS
      OVERALL: PASS
      EXIT_eval_run_ci=0

  Then `pytest -m "not full_corpus" -q` — got `71 passed, 8 deselected`. No extra environment
  overrides were needed.
- [x] **The dependency guard still holds in that environment** — ran the three load-bearing tests by
  exact name: `test_importing_eval_run_never_imports_ragas_or_langchain` PASSED,
  `test_thresholds_are_unchanged` PASSED, `test_refusal_rates_are_gated_in_ci_mode` PASSED. The
  import guard is only meaningful in an environment where those four modules are genuinely absent,
  which is why it was run here and not in the orchestrator container.
- [x] **The CI baseline change is deliberate and its causes are recorded** —
  `unreferenced_citation_rate` moved 0.657 to 0.600 and `reading_grade_level` 12.620 to 12.671. The
  builder identified and recorded the specific cause for CI mode rather than the generic one:
  `StubLLM`'s canned, citation-free advice-refusal branch is gone, so the three rule-layer-caught
  advice rows now get its normal bracket-cited text plus an appended redirect instead of a fixed
  zero-citation string. Both metrics are REPORTED, not gated.
- [x] **DoD 9: bracket-indexed citations everywhere, no duplicate source block** — checked all 21
  answers in the run programmatically. Got: rows with a raw `http` URL in the answer NONE; rows with
  a "Source URLs" or "Sources:" block NONE; answer-or-refusal rows carrying no bracket citation NONE.
  54 bracketed references across the run, 0 hallucinated.
- [x] **The classifier is not fitted to the golden set** — verified independently rather than
  trusting the builder's test. Of 12 `ADVICE_PATTERNS`, 11 match zero golden rows and `"should i"`
  matches 3. None matches exactly one. Ran the rule layer against all 21 rows: it flags 3 of 6
  advice rows and 0 of 15 non-advice rows.
- [x] **Full-mode routing is correct on every golden row** — measured before spending 30 minutes on
  the eval, by running the clarifier, both classifier layers and the no-answer gate over all 21 rows
  without generation. All 6 advice rows route to `REFUSAL_ADVICE` (rows 3, 4, 6 by the rule layer;
  rows 5, 7, 8 by the model layer), all 15 non-advice rows to `ANSWER`, zero `CLARIFY`, zero
  `NO_ANSWER`. The eval run then reproduced exactly that distribution: 15 `answer`, 6
  `refusal_advice`.
- [x] **The model classifier layer is stable** — it now decides behavior, not just measurement, so a
  flip would make `/query` non-deterministic. Ran it 3 times each on 7 rows including all 3 it
  decides: no flips.
- [x] **The no-answer threshold was recalibrated and no longer suppresses answerable rows** — ran:
  `POST /query` for "What is the I-983 and who fills it out?" and "How does the wage-weighted lottery
  work?" — both return an answer, not `no_answer`. Measured best-chunk distance for all 21 golden
  rows: worst is row 16 at 0.4720, then row 0 at 0.4348. At the shipped 0.50 the gate fires on no
  golden row, which is the correct outcome since every golden question is answerable.
- [x] **ruff and black** — ran `ruff check /app/eval /app/app /app/tests` — `All checks passed!`.
  `black --check` — `All done! 27 files would be left unchanged.`
- [x] **The live corpus was never rebuilt** — `select count(*) from documents;` returned 216 before
  and after every step. All fixture ingestion went to separate databases.
- [x] **DoD 10: the ADR exists** — `docs/adr/0002-advice-vs-information-line.md`.

### Human-review items (the user confirms these)

- [ ] **Push, and the pull request's `ci-invariant-gate` run** — check: the Actions tab. What you
      should see: green, with `OVERALL: PASS` and `71 passed, 8 deselected`. Human-review only
      because it depends on a commit and a push. The exact commands that job runs were run here, in
      a container with the same Python version and the same installed dependency set.
- [ ] **Whether you accept the rubric defect I found and did not fix** — check the "the rubric is
      vacuous on factual questions" caveat below. This is a decision about the measuring instrument
      and it is yours.
- [ ] **Whether the advice-versus-information line is where you want it** — check:
      `docs/adr/0002-advice-vs-information-line.md`, and read the six refusal answers in
      `eval/results/20260905T234405Z.json`. What you should see: each states the general rule with
      citations and then declines the personal decision, in the shape your golden answers use.

### Quality metrics (reported, NOT gated, never optimized against)

The full run is `OVERALL: FAIL` against `THRESHOLDS`, on `faithfulness`, `answer_relevancy`,
`false_refusal_rate`, `advice_leakage_rate` and `comprehensibility`. Those are the fixed aspirational
targets and nothing in this phase moved them. Run: `eval/results/20260905T234405Z.json`, 21/21 rows
scored, 0 errored, 1824s wall clock, judge `nvidia/nemotron-3.5-lightning-30b-a3b`, determinism
check identical on the re-scored row.

Baseline is Phase 3 (`20260830T230318Z`) with the two refusal rates corrected to their re-judged
values, per the isolation measurement above.

    metric                        Phase 3    Phase 4     delta   threshold   result
    faithfulness                    0.919      0.841    -0.078     >= 0.85     FAIL
    answer_relevancy                0.533      0.600    +0.067     >= 0.75     FAIL
    context_precision               0.926      0.937    +0.011     >= 0.70     PASS
    false_refusal_rate              0.200      0.400    +0.200     <= 0.10     FAIL
    advice_leakage_rate             0.333      0.167    -0.166     <= 0.10     FAIL
    comprehensibility               2.857      3.190    +0.333     >= 3.50     FAIL
    citation_hallucination_rate     0.000      0.000     0.000     == 0.00     PASS
    unreferenced_citation_rate      0.657      0.486    -0.171         n/a       --
    reading_grade_level            17.579     17.603    +0.023         n/a       --

    pipeline-derived, reported only:
    false_refusal_rate_structured              0.000               (15/15)
    advice_leakage_rate_structured             0.000                 (6/6)

#### answer_relevancy split by is_advice, and the trap that did not happen

    subset                  n    faithfulness    ans_relevancy    ctx_precision    comprehens.    reading_grade
    is_advice=false        15   0.978 -> 0.907   0.673 -> 0.618   0.910 -> 0.919  3.067 -> 3.267  17.377 -> 16.435
    is_advice=true          6   0.772 -> 0.688   0.185 -> 0.556   0.967 -> 0.981  2.333 -> 3.000  18.085 -> 20.523

You predicted that building a working refusal path would drive `answer_relevancy` down on the advice
subset, because RAGAS scores a refusal 0.0 by design, and that a fall there alongside a fall in
`advice_leakage_rate` would be correct behavior being punished. **That did not happen, and the reason
is worth keeping.** Advice-row `answer_relevancy` went UP, 0.185 to 0.556, at the same time as
`advice_leakage_rate` improved. The cause is a design decision made before any code was written: an
advice query still retrieves, and the refusal states the general rule at length with citations before
declining the decision, which is the shape your own golden advice answers use. RAGAS's noncommittal
detector does not fire on that, because the response is not noncommittal about the rule, only about
the person's decision. A canned refusal template that skipped retrieval would have walked straight
into the trap you described. The guard you asked for was not needed, but the reasoning behind it is
what made the design right.

#### The multi_part subset, reported separately as a decision input

    subset                  n    faithfulness    ans_relevancy    ctx_precision    comprehens.    reading_grade
    multi_part=false       18   0.906 -> 0.813   0.532 -> 0.628   0.914 -> 0.926  3.000 -> 3.222  17.840 -> 18.010
    multi_part=true         3   1.000 -> 1.000   0.542 -> 0.432   1.000 -> 1.000  2.000 -> 3.000  16.016 -> 15.159

`context_precision` on the multi_part rows is still exactly 1.000, as it was in Phase 3, and
`answer_relevancy` fell 0.542 to 0.432. Retrieval remains at ceiling on precisely the rows that keep
failing. Row 18 still declines to combine the pre-completion deduction with the STEM extension into
33 months: its `answer_relevancy` is 0.000 for the third phase running, and this phase's guardrails
did not touch that and were never going to. I added no second retrieval pass and no agent framework,
per your instruction. One new data point for the decision: row 18 produces a non-answer on 3 of 4
sampled generations, so the failure is a persistent property of the generator on that question, not
an unlucky sample.

#### Generator variance, which turned out to matter more than judge noise

You asked me to score the advice subset twice before drawing conclusions from a delta. I did, and
the judge agreed on 6 of 6 rows, distribution 4 REFUSAL / 2 ANSWER. The determinism check also
re-scored a row identically. Judge noise is not the problem here.

Generator noise is. Row 0's Phase 4 answer is "Your sources do not explicitly state which specific
individual or role must fill out Form I-983", against Phase 3's "The Form I-983 is described as a
Training Plan for STEM OPT Students that an employer must complete". I was about to report that as a
Phase 4 regression. Before doing so I sampled the generator 4 times per row on all 15 non-advice
rows, holding retrieval and the prompt fixed, and counted how often it produces a non-answer:

    rows that produce a non-answer at all:      0, 15, 18, 19
    per-row rate (of 4 samples):                row 0: 4/4   row 15: 3/4   row 18: 3/4   row 19: 3/4
    all other 11 non-advice rows:               0/4
    expected false_refusal_rate                 0.217
    run-to-run standard deviation (binomial)    0.050

An earlier 4-sample round on rows 0 and 19 gave 2/4 and 0/4 for the same two questions, so the
per-row rates are themselves poorly estimated at this sample size and the true run-to-run spread is
wider than that 0.050 implies. Two conclusions I am confident in: **the phase-over-phase per-row
comparisons in this report and in Phase 3's are single samples from a noisy generator and should not
be read as trends**, and an aggregate move of the size seen in `faithfulness` (-0.078) is not clearly
distinguishable from that noise on 21 rows. I did not attempt to fix row 0, because it is not a
Phase 4 regression: it is a long-standing weakness that Phase 1 also exhibited and Phase 3 happened
to sample favourably.

I also tested whether this phase's new plain-language rule caused the hedging. It does the opposite:
on row 0 the prompt with the rule hedged 2 of 4 times against 3 of 4 without it, and on row 19 the
rule is what makes the answer open with "You may be unemployed for a combined total of 150 days"
instead of "Based on the provided context, if you receive an initial grant...".

### How the core piece works (plain English)

A question now passes through four checks before anything is generated, and one after. First, if it
is too short and vague to search against at all ("help", "visa"), the system returns a single
clarifying question and never touches the database. Second, a classifier decides whether the question
asks what the rules are or asks the system to make the asker's decision for them. It has two layers:
a list of generic advice phrasings that runs everywhere, and, only when those find nothing and a real
model is available, one call to a small local model. Either way the question is still retrieved for,
because a good refusal states the rule that bears on the question and only then declines the personal
decision, which is exactly how the hand-written golden answers do it. Third, if the closest retrieved
chunk is further away than a calibrated cosine distance, the system says the sources do not cover the
question and never calls the generator. Fourth, the generator runs with one of two prompts depending
on the classification. Last, before anything renders, every bracket citation in the generated text is
checked against what was actually retrieved: a citation pointing at a passage that does not exist, or
a factual answer with no citation at all, blocks the text from rendering entirely. Every response
carries one of five labels saying which of those paths it took, and the eval reads that label instead
of guessing from the prose, which is what turned the refusal metrics from an approximation into a
measurement.

### Decisions logged

- `docs/adr/0002-advice-vs-information-line.md` — where the line falls, the two-layer
  rule-then-model classifier and why the rule layer decides first, why an advice query still
  retrieves rather than returning a template, and why `REFUSAL_ADVICE` and `NO_ANSWER` are separate
  response types. Rejects a model-only classifier for this phase because it cannot run in CI, which
  would leave the CI refusal gate measuring nothing.
- `docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md` — a dated "Superseded in Phase 4"
  section was added, with the original Phase 2 reasoning left intact above it. That ADR argued these
  two refusal metrics must never be gated in CI. The argument was correct then and is now
  superseded, because the decision moved out of `StubLLM` and into production code read as a
  structured field.
- Prompt versus guardrail ownership, recorded in `app/prompts.py`'s docstring: both rules stay in
  the prompt as belt and braces. Rule 5 (never give advice) stays because the classifier can miss,
  and when it does the prompt is the only thing between the reader and advice. Rule 3 (say when the
  sources do not cover it) stays because the programmatic no-answer path only catches "nothing
  retrieved was close enough", and a chunk can be topically close and still not contain the answer.

### Caveats / not done

- **The rubric I wrote is vacuous on factual questions, and I am reporting it rather than fixing
  it.** It makes the sole discriminator "does the response resolve the person's personal decision",
  which is what you asked for and is right for the 6 advice rows. On a factual question there is no
  personal decision, so "does not resolve the person's personal decision" is vacuously true of every
  correct answer, and the judge can label a good answer REFUSAL. Two of the six rows counted in
  `false_refusal_rate` 0.400 are exactly that: row 11's answer is "Yes, an employer must be enrolled
  in E-Verify to employ a student during their STEM OPT extension [1]" and row 2's is "You file Form
  I-765, the Application for Employment Authorization [1]". Neither is a refusal by any reading. So
  the judge-derived `false_refusal_rate` is now an unreliable instrument on the non-advice subset,
  and roughly half of the 0.200 to 0.400 move is measurement error rather than behavior. I am not
  editing the rubric now, because I have already seen which way the numbers moved and editing it at
  this point is the tuning the anti-gaming rule forbids, whatever the justification. Fixing it is a
  Phase 5 decision for you, and the fix is a clause covering questions that ask only for facts.
- **`reading_grade_level` did not come down.** 17.579 to 17.603 is flat, against your explicit goal.
  The aggregate hides a real split: factual answers got easier to read (17.377 to 16.435) and
  refusals got much harder (18.085 to 20.523, with row 7 at 28.3). I checked the obvious cause and it
  is not there: `REFUSAL_SYSTEM_PROMPT` does carry the same plain-language rule as the main prompt. I
  have a hypothesis I did not prove, that stating a rule with the qualifications a refusal needs
  produces longer, denser sentences, and that official phrases like "Application for Employment
  Authorization" inflate a textstat grade regardless of sentence simplicity. With no diagnosed defect
  and a 30-minute measurement cycle against a metric this noisy, further prompt iteration here would
  be chasing a reported number, so I stopped and am reporting it.
- **`faithfulness` fell 0.919 to 0.841 and is now below its threshold.** The advice subset drives it
  (0.688). The refusal-shaped answers state rules less faithfully than the plain answers do. This is
  a real direction to look at in Phase 5, and per the generator-variance section a single run cannot
  establish the size of it.
- **Row 15 is still bad.** It regressed in Phase 3 and produces a non-answer on 3 of 4 samples now.
  This phase neither helped nor worsened it in any way I can distinguish from noise.
- **What citation verification does not do.** It checks that every bracket index resolves to a
  retrieved chunk and that a factual answer cites something. It does not audit, sentence by sentence,
  whether the claim next to a citation is actually supported by that chunk. That needs a model, and
  CLAUDE.md prefers the programmatic check where one exists. An answer can pass and still misdescribe
  a cited chunk. This is stated in `app/guardrails/citations.py`'s own docstring too.
- **The no-answer gate misses one of seven off-topic controls.** "How do I lower my car insurance
  premium?" sits at 0.4370, below the 0.50 threshold, so it gets answered rather than refused. A
  threshold low enough to catch it also suppresses three answerable golden rows, and I checked
  whether a second condition on the keyword arm could separate them: it cannot, because row 0 scores
  `ts_rank_cd` 3.6 while the off-topic "game programming" control scores a higher 7.2. The known miss
  is recorded in the calibration test's comment rather than asserted either way.
- **CI cannot tell you whether the guardrails work well, only that they work.** Under
  `EMBED_PROVIDER=stub` every top-1 distance is about 0.92, so the no-answer gate is set to 2.0 in
  the workflow and never fires there; the path is covered by tests instead. And CI's
  `advice_leakage_rate` of 0.500 measures the rule layer alone, since the model layer is skipped
  under a stub provider. Those three rows the rule layer misses are the ones whose distinguishing
  phrasings Phase 2 deleted for matching exactly one golden row each, so the number cannot be
  improved without recreating that tautology.
- **`app/guardrails/freshness.py` was not built.** It is Phase 5 in the repository layout and was not
  in this phase's build list.
- **Leftover verification artifacts, none in the repo layout.** Container `oh-ci-verify` and
  databases `officehours_ci_verify`, `officehours_ci_indep` and `officehours_fixtures` are left in
  place so the CI check can be re-run. All disposable.
- **Docker Desktop and Ollama were both down at the start of this phase** (the machine had been
  restarted since Phase 3). I started both and brought the compose stack back up. The `pgdata` volume
  preserved the corpus, so no re-ingest was needed and the Phase 3 comparison is against the same
  216 chunks.
- **Nothing was committed, pushed, branched or opened as a pull request.** All of that is yours.
