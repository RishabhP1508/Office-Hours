# 0019: Dated-rule companion retrieval, and why n=2 is a fitted constant

## Context

The DHS fixed-period-of-admission final rule takes effect Sept 15 2026 and changes the F-1
post-completion departure period from 60 days to 30. The corpus already holds both rules: the
still-current 60-day grace period (for example chunk 506, "the 60-day departure preparation period
commonly known as the 'grace period'") and the fixed_admission FAQ's dated replacement (for example
chunks 702/708/710, all carrying `rule_effective_date = 2026-09-15`). ARCHITECTURE.md requires an
answer to state both, with their dates, whenever the corpus holds both -- never pick one silently.

Measured directly: "What is the grace period after OPT ends?" retrieves a fused top-5 that already
contains *some* dated chunk (671, "What happens if I plan to travel when filing for
post-completion OPT or STEM OPT?"), but not the one that actually states the new number. Chunk 702
("What is the new departure period for F students?", "F students now have 30 days to depart the
United States...") loses the RRF fusion race outright and never reaches the generator. The
freshness notice guardrail already fires here -- it only needs one retrieved chunk to carry
`rule_effective_date` -- but firing a notice is not the same as the generator having the sentence
that states the replacement number in front of it.

## What was measured

Four alternatives were tried first and rejected on measurement, not on suspicion:

- **Lower `RRF_K` toward 1.** Fails on two of the three queries at every value down to 1, and the
  reason is NOT that the chunk is missing from the candidate pool. Chunk 702's whole-corpus semantic
  rank on the three failing phrasings is 2, 7 and 4 out of 221, so it is inside
  `HYBRID_CANDIDATE_POOL=20` and fused on every one of them. It loses to chunks that beat it *in the
  same arm*: on "How long do I have to leave the US after OPT ends?" the winner is chunk 671 at
  semantic rank 1 against 702's 4, and on "What is the grace period after OPT ends?" the winners sit
  at semantic rank 2 and 8 against 702's 7. Lowering `RRF_K` sharpens the reward for a good rank,
  which helps rank 1 more than it helps rank 4; it cannot promote a chunk past one that already
  outranks it in the arm it is strong in. (The "never a candidate" story is the exact mistake entry
  8 of REPORT.md's instrument table records, arrived at a second time; the ranks above are measured,
  not inferred.)
- **A floor on a single arm** (e.g. "always admit the closest chunk by raw semantic distance").
  Fixes none of the three failing queries: chunk 702 is not the single closest chunk on any of
  them (chunk 670 is closer on "What is the grace period after OPT ends?", and other non-dated
  chunks are closer still on the other two), so a floor keyed on "closest overall" surfaces the
  wrong chunk.
- **Raise `RETRIEVAL_TOP_K` to 10.** Still fails one of the three queries (chunk 702 sits outside
  even a widened top-10 for one phrasing), and it collides directly with
  `test_retrieval_top_k_defaults_to_five`, the guard that keeps the generator's context size fixed
  against the Phase 1 semantic-only baseline comparison (`eval/results/20260830T183110Z.json`).
  Raising it would also inflate context size for every query, not just the ones with a dated rule
  in play.
- **Alias the query with a synonym for the missing term.** Has no word to add: chunk 702 does not
  lack a term the query is missing. The query "grace period after OPT ends" already contains
  "grace period" verbatim, and chunk 506 (which states the *current* rule) contains it too --
  aliasing cannot make chunk 702 rank higher on a term it already loses on. Measured directly on
  `ts_rank_cd` for "What is the grace period after OPT ends?" (tsquery `'grace' | 'period' | 'opt' |
  'end'`): chunk 702 scores 3.8 and ranks 30th, while the arm is won by chunk 456 ("Recommend OPT")
  at 26.2 and chunk 444 at 10.6. Neither winner contains the phrase "grace period" at all; they win
  on sheer density of "opt" and "period" across a long chunk. Chunk 506, which does contain "the
  60-day departure preparation period commonly known as the 'grace period'", scores 7.2 and still
  beats 702. No alias changes a density gap, because the alias would add a term the chunk already
  has.

All four alternatives operate inside the existing single fusion pass and none of them fixes the
motivating case. The fix that does work has to look at the *content* of what fusion already
retrieved (a `rule_effective_date`), then go get more content sharing that same signal --
something a ranking tweak inside one fusion pass cannot do, because ranking has no notion of "this
row is dated, go find its siblings."

## Decision

`app/db.py::hybrid_search` gains two CTEs after the existing RRF fusion, `top` and `companions`,
inside the same single SQL statement:

- `top` freezes today's `ORDER BY rrf_score DESC, id LIMIT k` -- unchanged ranking, just given its
  own name so `companions` has something concrete to compare against.
- `companions` finds up to `Settings.DATED_RULE_COMPANIONS` more chunks that carry a
  `rule_effective_date` equal to one already present in `top`, ordered by raw cosine distance, and
  not already in `top`.

The final result is `top`'s rows (unchanged order, `retrieved_by="fusion"`), followed by
`companions`' rows (`retrieved_by="dated_companion"`). `app/pipeline.py`'s no-answer gate reads only
`retrieved_by == "fusion"` chunks for its minimum-distance calculation, on purpose: a companion is
admitted because a dated rule is already in play in the fusion result, not because it is relevant
to the question on its own merits, so it must never be able to turn a genuine "sources don't cover
this" into a confident answer.

`DATED_RULE_COMPANIONS=0` (or no dated chunk anywhere in `top`) makes `companions` return zero rows
by construction, and the query's output is byte-identical to before this feature existed.

**n=2 is fitted, and that is stated plainly, not hidden.** The number was chosen by running the
eight-query acceptance ladder (the three failing departure-period phrasings, plus five control
queries with no dated content) at n=1, n=2, and n=3, and picking the smallest n that passes all
eight. On "What is the grace period after OPT ends?" specifically, the single closest dated chunk
by distance is 670 (discusses the transition but never states "30 days"); n=1 admits 670 alone and
still fails to surface the number. Chunk 702 is the *second*-closest dated chunk on that query, so
n=2 is the smallest value that reaches it. n=3 was also tried and adds nothing further on any of
the eight queries, so it was not adopted -- it would only add a third, unused source per query.

The acceptance ladder is a fixed, external acceptance test (three real failing queries, phrased
before n was chosen, plus controls to make sure the fix does not fire where it should not), not a
quality score being chased, so fitting the implementation to it is the legitimate use of a
measurement CLAUDE.md's eval carve-out describes -- this is a functional/safety gate ("does the
retrieved set contain the passage that states the new rule"), not `eval/golden.jsonl` or a judge
score. But n=2 is still a number chosen because it happened to pass this specific ladder, on this
specific corpus, today. The honest test of whether 2 generalizes is the next dated rule that lands
in this corpus, not this one -- if a future rule change needs 3 dated chunks apart by distance
before reaching the one that states the number, this constant will need re-measuring the same way,
not assumed to still hold.

## Why this is not the second retrieval pass ADR 0003 rejected

`docs/adr/0003-no-second-retrieval-pass.md` rejected decomposing a question into sub-questions and
re-retrieving for each, specifically because that component's own motivating case (golden row 18)
was measured and shown to still miss the target chunk after decomposition -- a component that does
not fix the case it exists for is not worth the moving parts.

This is a different shape of change, not a smaller version of the same one:

- **No query decomposition.** The question is embedded once, exactly as it always was; nothing
  about it is broken into sub-questions.
- **No model call.** `companions` is pure SQL over already-known facts (`top`'s ids and their
  dates); nothing here asks a model anything.
- **No second round trip.** `companions` is a CTE in the same single statement `hybrid_search`
  already sends -- one query, one connection round trip, same as before.
- **A deterministic merge policy**, not a heuristic one: exact date match, exclude what is already
  present, order by distance, cap at n. Nothing probabilistic or model-mediated decides what gets
  added.
- **Its motivating case is measurably fixed.** Unlike ADR 0003's second pass, which was measured
  and shown NOT to retrieve golden row 18's missing chunk even after decomposition, this change was
  measured and shown TO retrieve chunk 702 (and the equivalent for the other two failing phrasings)
  once companions are turned on. The distinction ADR 0003 draws -- reject a component whose own
  motivating case it would not have fixed -- is exactly the bar this change was held to before being
  built, not after.

## Tradeoff

This fires on any query whose fused top-k already contains a dated chunk, regardless of whether the
question is actually about the dated rule's *content* -- measured at 3 of the 21 golden rows and 1
of the 14 control queries used to calibrate `NO_ANSWER_MAX_DISTANCE`. Each of those rows gets up to
2 extra sources attached that the generated answer will probably not cite, on top of an
`unreferenced_citation_rate` already measured at 0.686. This change makes that number worse, not
better, in exchange for the departure-period questions it fixes. That is an accepted cost: an
unreferenced source sitting unused in the context is a quality cost; an answer that silently omits
one of two true, dated rules is the safety failure ARCHITECTURE.md's "answers state both the current
rule and its dated replacement" exists to prevent.

## Alternatives considered

See "What was measured" above for the four alternatives tried and rejected on evidence before this
design: lowering `RRF_K`, a single-arm distance floor, raising `RETRIEVAL_TOP_K`, and query
aliasing. A fifth, briefly considered and dropped without building it: reusing the second-retrieval-
pass machinery ADR 0003 already rejected, gated only to fire when a dated chunk is present. Rejected
for the same reasons ADR 0003 gives (a model call and a second round trip to fix something a
deterministic SQL merge already fixes more cheaply), not because it would not have worked.
