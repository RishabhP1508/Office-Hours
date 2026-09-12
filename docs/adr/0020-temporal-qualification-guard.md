# 0020: The temporal qualification guard, and exactly what it does not cover

## Context

The DHS fixed-period-of-admission rule takes effect 15 September 2026 and changes the F-1
post-completion departure period from 60 days to 30. ADR 0019 fixed the retrieval half: the passage
stating the new rule now reaches the generator. This ADR covers what the generator then does with it.

Measured over nine generations against three real phrasings, with the correct passage in context:
every run stated both numbers, and **six of the nine attached the effective date to neither the claim
nor its sentence**. The date was present in the response, but only in the freshness notice appended
at the very end. A reader sees "The departure period for F-1 students is now 30 days" as the
substantive claim and a generic trailing sentence below it.

Three attempts had already been made to fix this by instructing the model: prompt rule 4, the
passage-level dated note (`app/prompts.py::_rule_date_note`), and a sentence-level annotation naming
the passage's own "now". The third was measured against a pre-declared target of 9 of 9 and scored
3 of 9, and was removed. This project's stated preference is a programmatic check over a model
judgment, and three failed attempts at instruction is enough evidence to act on it.

## What was measured

**The obvious check cannot work, and this is worth recording because it was designed, reviewed and
approved before anyone noticed.** The natural formulation is "if the answer states a figure that
appears only in future-dated passages, the answer must also state the effective date".
`app/pipeline.py` step 8 already appends the freshness notice, which contains that date, to
`answer_text` for every ANSWER and REFUSAL_ADVICE. An answer-scoped check would therefore have passed
on every input ever given to it and reported clean. **The check has to be sentence-scoped.**

**Three figure-extraction defects, each found by running the guard against the live 221-chunk corpus
and reading which figures it treated as future-only, not by reading the code.**

1. `_DATE_PHRASE_RE` matched only full month names. **The full form appears in zero dated chunks.**
   The corpus writes `Sept. 15, 2026`, `Nov. 14, 2030`, `Sept. 14, 2026`, so date digits leaked in as
   figures: `"...effective Sept. 15, 2026."` yielded `['15', '30', '60']` where the full form
   correctly yielded `['30']`.
2. URLs leaked digits. `https://i94.cbp.dhs.gov/home` in chunk 675 was the entire source of the
   figure `94`.
3. Numeric dates leaked digits. The `Last Reviewed/Updated: 01/30/2026` footer, present on chunks
   439, 444, 492, 515, 600 and 660, yielded `30`. This one mattered most: chunk 444 is retrieved for
   a real acceptance query, so the footer disqualified `30` from being future-only and **the guard
   went silent on a query it was built to catch.**

**Coverage, measured after all three fixes, on the eight-query acceptance ladder.** "Has power" means
the 30-day figure is genuinely future-only given that query's real retrieved set, so the guard is able
to fire at all:

    has power:  A, B, C, E, F, G, H        7 of 8
    silent:     D                          1 of 8

    also silent: "How many days do I have to depart the US after my F-1 program ends?"
                 "When do I have to leave the US after my program ends?"

**Why D is silent, and why no string test fixes it.** The blocking chunks are undated and contain the
same digits for a different rule:

    chunk 453  "must file within the 30-day period after your DSO OPT recommendation"
    chunk 528  "M students have 30 days after completion of their program"

These are homonyms: two real rules that share a number. The definition of "future-only" is
digit-level, so an unrelated retrieved rule using the same figure disqualifies it. Nothing in the
answer text distinguishes them either. Closing this needs matching the figure together with its unit
and subject, or matching the answer's sentence back to a specific chunk's provenance, which is a
materially larger algorithm than this one.

**Against the six measured failure modes**, taking each query's real retrieval into account:

- **Detects 5 of 6.** The three position failures on query F, and both failures on query G.
- **Misses 1 of 6 entirely**: D2, because the guard has no power on query D at all.
- **Fully remediates 4**: the position failures, where the only defect is the missing date, so
  supplying it makes the answer correct.
- **Leaves a contradiction standing on 1**: G0 asserts the new rule is already in force. The inserted
  sentence contradicts that claim rather than removing it, because removing it would mean rewriting
  the model's own assertion, which is a semantic judgment a string test cannot make.

An earlier draft of this ADR claimed "detects 6 of 6". That was derived by evaluating the detector's
logic against the nine runs' sentences without checking whether the figure was future-only given each
query's actual retrieved set. It was wrong, and it is recorded here rather than quietly corrected.

## Decision

`app/guardrails/temporal.py::qualify_future_dated_figures` runs in `app/pipeline.py` step 8 for
ANSWER and REFUSAL_ADVICE. For each sentence containing a figure that appears only in future-dated
retrieved chunks and that does not itself state that rule's effective date, it inserts one derived
sentence immediately after that sentence.

**It inserts rather than blocks.** The answer is substantively correct and missing a qualification;
blocking would discard a correct answer and show the reader nothing. That is different from
`app/guardrails/authority.py`, where the output itself is compromised and blocking is right.

**It inserts after the sentence rather than appending at the end.** Appending at the end is what the
system already does with the freshness notice, and a trailing qualifier is precisely the behaviour
measured as insufficient.

**Placement, and why it is exactly there.** After the `parse_cited_indices(answer_text)` call that
feeds `build_freshness`, so the insertion provably cannot change `cited_indices`; and before the
notice append, so the detector only ever sees generated prose rather than system-appended text.

**The no-bracket property is asserted, not assumed.** `app/pipeline.py:738-741` records that the
freshness notice can safely be appended after `verify_citations` because it carries no citation
bracket. The inserted sentence needs the same property, and tests assert both that it contains no
bracket and that `parse_cited_indices` is unchanged across the insertion.

**A cache staleness bug is fixed at the root as part of this.** `app/cache.py::corpus_version` was
derived from `max(last_changed_at)` and `count(*)` with no date component and no TTL, so any
date-derived text baked into a cached `answer_text` would be served unchanged forever. This was
already live for the freshness notice: a response cached on 11 September saying "takes effect on
September 15, 2026" would still say it on 16 September. `corpus_version` now folds in the UTC date.
The honest cost is that cache entries expire daily; the cache is a cost optimisation and a wrong date
is a correctness problem.

## Tradeoff

The guard is digit-level, so its coverage depends on what else retrieval happened to return. It is
silent whenever an unrelated retrieved rule shares the figure, which is 1 of 8 ladder phrasings today
and could be more or fewer tomorrow as the corpus changes. It is a backstop that catches the clean
cases, not a guarantee.

Verification also has a known weakness. The false-positive pass over 1,407 stored answers reported
zero firings, and that number is close to meaningless: all 21 golden questions have empty future-only
figure sets, so the corpus barely exercised the check. It establishes that the guard does not fire
spuriously on real answers; it establishes nothing about the catch rate, and reporting one from that
corpus would be the defect rather than the measurement. What does carry weight is the pipeline-level
mutation test (neutering the detector makes the unqualified sentence render, restoring it makes the
qualification return) and the byte-exactness check (1,407 of 1,407 answers with zero insertions are
returned byte-identical, which matters because the sentence splitter was modified).

## Alternatives considered

**Block and return BLOCKED_UNVERIFIED.** Rejected: disproportionate to an omission, and it would
replace a correct answer with an error screen.

**Regenerate once with a corrective instruction.** Rejected: a second generation call, non-deterministic,
and instruction is the approach that has already failed three times here.

**Match the figure with its unit and subject, or match sentence provenance to a chunk.** This is the
only thing that closes the homonym gap. Not built: materially larger, and 15 September is four days
away. Recorded as the known route if the gap needs closing later.

**Keep the sentence-level prompt annotation alongside this.** Rejected: it was measured against a
pre-declared target of 9 of 9, scored 3 of 9, and was removed. Keeping a change that failed its own
acceptance test because one run looked good is the failure mode this project exists to avoid.
