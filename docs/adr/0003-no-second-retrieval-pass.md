# 0003: No second retrieval pass, and the real reason row 18 fails

## Context

Three golden rows are labelled `multi_part`: they ask a question whose answer has to be assembled
from two or more separate facts. Row 18 is the hard one:

> If I use three months of full-time pre-completion OPT, how much time do I get after graduating
> including a STEM extension?

Its hand-written answer is 33 months: three months of full-time pre-completion OPT comes out of the
12-month allowance, leaving 9 months of post-completion OPT, plus a 24-month STEM extension.

Row 18 has scored `answer_relevancy` 0.000 in every full run so far, Phase 1, Phase 3 and Phase 4,
while scoring `faithfulness` 1.000 and `context_precision` 1.000 in all three. Phase 4 sampled the
generator four times on it and got a non-answer on three of four. The obvious next move, and the one
this project deliberately did not make, is a bounded second retrieval pass: break the question into
sub-questions, retrieve again for each, and hand the union to the generator.

The case for skipping it, as it stood before this phase, was that `context_precision` is exactly
1.000 on all three `multi_part` rows in both Phase 3 and Phase 4, so retrieval is already at ceiling
on the rows that fail and a second pass would have nothing left to fetch.

That reasoning is wrong, and this ADR records what is actually happening, because the wrong reason
would have sent the next fix to the wrong place.

## What was measured

**`context_precision` 1.000 does not mean retrieval is at ceiling.** It is a precision metric. It
says every chunk that came back was relevant. It says nothing about whether the chunk carrying the
missing fact came back at all, which is recall. Row 18 has a real recall failure hiding behind a
perfect precision score.

**The sentence that answers row 18 is in the corpus and is not retrieved.** The general rule lives
in chunk 433, the pre-heading chunk of the USCIS OPT page:

> However, all periods of pre-completion OPT will be deducted from the available period of
> post-completion OPT.

For row 18's question, chunk 433 is keyword rank 22 and is absent from the semantic arm's 20-deep
candidate pool, so it does not appear in the fused top 25, let alone the top 5 the generator sees.

**What the generator does see is not enough to answer.** The retrieved top 5 is chunks 435, 441, 437,
511 and 434. Chunk 434 states the deduction rule only through two worked examples, both for a full
year: one year of part-time pre-completion OPT reduces the remaining full-time period by 6 months,
one year of full-time reduces it by a year. Neither licenses prorating three months. Phase 4's actual
answer says exactly that, and cites the chunk it says it:

> Your context explains that using one full year (either part-time or full-time) reduces your
> available post-completion OPT, but it does not specify rules for a three-month period [5].

That is prompt rule 3 working. The model declined to extrapolate a proportional rule from two
worked examples, which is the behavior this project asks for everywhere else. Row 18's
`answer_relevancy` 0.000 has been measuring the system doing the right thing with an incomplete
context, for three phases.

The Phase 5 run (`eval/results/20260906T020422Z.json`) states the diagnosis in the model's own
words, without being asked to:

> The rules described state that if you have received one year of full-time (40 hours per week)
> pre-completion OPT, your entitlement to any post-completion OPT is reduced by 100% [5]. For
> partial periods less than the full academic equivalent, the sources do not specify a calculation
> method.

That is exactly the gap: the retrieved set has the one-year examples and not the general rule. The
same run scored that answer `answer_relevancy` 0.915, against 0.000 for the same shape of answer in
each of the three previous phases. Nothing about the answer improved. RAGAS's noncommittal detection
is simply unstable on this row, which is one more reason not to have treated the 0.000 as a signal
worth building a retrieval component against.

**A second retrieval pass would not have found the missing chunk either.** A second pass retrieves
with the same embedder against the same index, so it inherits the same failure. Measured against the
live corpus, a sub-question aimed as directly at the missing fact as it is possible to aim,
"Is pre-completion OPT deducted from post-completion OPT?", returns chunks 434, 437, 610, 462 and
456. Chunk 433 is not among them. Two other decompositions of the same question also miss it.

**The cause is upstream of retrieval.** Chunk 433 is 874 characters and roughly half of them are a
USCIS site-wide alert about photo submission requirements, mounted and unretouched images, and
Application Support Center visits. That text has nothing to do with OPT duration and it dilutes the
chunk's embedding. Removing that one paragraph and re-embedding the same remaining text moves it
from cosine distance 0.3535 to 0.3039 against the sub-question above, which would put it ahead of
chunk 434 at 0.3209 instead of behind it. Against row 18's real question it moves from outside the
top 25 to 0.2663, against a current fifth-place distance of 0.2625.

## Decision

Do not build a second retrieval pass. Record the diagnosis instead.

The fix that the evidence points at is in ingestion, not in retrieval orchestration and not in
generation: `app/ingest.py::remove_noise` strips tags whose id or class matches a list of noise
keywords, and "alert" is not on that list, so a site-wide banner survives into the content chunk. A
chunk that is half boilerplate embeds as half boilerplate.

Nothing about that is built in this phase, and it is deliberately not built here. Phase 5's scope is
freshness. This is written down so the next person to look at row 18 starts from the measurement
rather than from the metric name.

## Tradeoff

Not building the second pass leaves row 18 answering with a non-answer for at least another phase,
and leaves `answer_relevancy` low on a row where a reader would reasonably expect an answer. That is
a real cost and it is visible in every eval run.

Against that: a second retrieval pass adds a decomposition step that needs a model call, a second
round trip to Postgres per sub-question, and a merge policy for the two result sets, and the
measurement above says it would not have fixed the row it was being considered for. Building it
would have added moving parts and left the defect in place, while making the eval numbers slightly
harder to attribute.

The honest limit of the ingestion fix: stripping the alert moves chunk 433 from outside the top 25
to roughly the edge of the top 5, at 0.2663 against a fifth place of 0.2625. That is necessary but
possibly not sufficient. It may also need the keyword arm to promote it, or `RETRIEVAL_TOP_K` to be
larger, and `RETRIEVAL_TOP_K` is currently fixed at 5 to keep the Phase 1 baseline comparable. Nobody
should read this ADR as saying one deletion fixes row 18.

## Alternatives considered

**Fix it in the prompt.** Tell the generator it may prorate a rule stated through worked examples.
Rejected. It would license exactly the extrapolation this system exists to refuse, on a topic where
a wrong number costs someone their status, and it would apply to every question rather than this one.

**Raise `RETRIEVAL_TOP_K`.** Cheap to try, but chunk 433 is fused rank 26 or worse for row 18, so
top_k would have to roughly quintuple, dragging 20 more chunks of context into every question to fix
one. It also breaks the Phase 1 baseline comparison, which is held fixed on purpose.

**Add the missing sentence to the golden set's source list, or soften row 18's ground truth.**
Rejected outright. The golden set is the standard, it is hand-written by the user, and editing it to
match what the system does is the exact failure CLAUDE.md's eval carve-out exists to prevent.

**Build the second pass anyway, as a demonstration.** Rejected. A retrieval component whose own
motivating case it does not fix is a component that has to be maintained and explained forever.
