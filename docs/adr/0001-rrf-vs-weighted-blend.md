# 0001: Fuse by rank (RRF), not by a weighted blend of scores

## Context

Phase 3 needs to combine two rankings of the same 216-chunk corpus into one ordered list: a
semantic ranking from pgvector cosine distance, and a keyword ranking from `ts_rank_cd` against a
generated `tsvector` column. These questions mix meaning ("can I work while my extension is
pending") with exact tokens ("I-983", "cap-gap", "24-month"), and neither ranking alone covers both.

The two scores have no common scale. Cosine distance is bounded 0 to 2 (identical vectors score 0).
`ts_rank_cd` is unbounded above and has no fixed floor: probing the live corpus, the query "What is
the I-983 and who fills it out?" produces `ts_rank_cd` scores from 3.6 down to 0.4 across its
matching rows, and a different query produces a different range entirely, because the score depends
on term frequency and document length, not on any property shared with cosine distance.

## Decision

Fuse the two rankings by RANK, with Reciprocal Rank Fusion, not by SCORE with a weighted blend.

Each arm's top `HYBRID_CANDIDATE_POOL` (20) candidates are numbered 1, 2, 3, ... by `ROW_NUMBER()`.
A chunk's fused score is `1/(RRF_K + semantic_rank) + 1/(RRF_K + keyword_rank)`, treating a missing
rank (the chunk did not appear in that arm's top 20) as contributing 0. `RRF_K` is 60, the constant
from the original Cormack/Clarke/Buettcher RRF paper, not tuned against this corpus or the golden
set. The whole computation is one CTE (`app/db.py::hybrid_search`), so it costs one round trip.

RRF needs no normalization step and no weight to tune: a rank is already a small positive integer
in both arms, so `1/(k + rank)` is directly comparable between arms without ever looking at the
underlying distance or `ts_rank_cd` value.

## Tradeoff

RRF throws away margin information. A chunk that is a runaway best semantic match (distance 0.05,
next-closest 0.4) gets exactly the same `1/(60+1)` contribution as a chunk that just barely
scraped into rank 1 (distance 0.38, next-closest 0.39) would have gotten. With `RRF_K=60` over a
20-deep pool, the within-arm spread across those 20 ranks is small (`1/61` down to `1/80`) next to
the roughly `1/61`-sized bonus a chunk gets for appearing near the top of the *other* arm too. That
is RRF's built-in bias toward cross-arm agreement, and it is a deliberate choice, not an accident of
the formula: a chunk two independent signals agree on outranks a chunk only one signal loves, even
if that one signal loved it more.

The concrete case this corpus produces: for "What is the I-983 and who fills it out?", chunk 468
(a Presidential Proclamation page on restricting entry of certain nonimmigrant workers) is semantic
rank 1 and keyword rank 3, because it happens to contain the incidental word "fill". Chunk 444 (the
actual STEM OPT chunk that names Form I-983) is keyword rank 1 but does not appear in the semantic
arm's top 20 at all. Under RRF, chunk 468 still outranks chunk 444 overall, because it is present
and reasonably ranked in both arms while chunk 444 is present in only one. RRF does not fix this by
putting the right chunk first; what it does is surface chunk 444 into the top 5 at all, something a
semantic-only ranking never would have done, since chunk 444 is absent from that ranking entirely.
Getting the right chunk into the top `RETRIEVAL_TOP_K` is what the generator needs; RRF delivers
that here, but a reader should not expect RRF to always rank the single best chunk first when the
two arms disagree about what "best" means.

## Alternatives considered

- **A weighted blend of normalized scores** (e.g. min-max normalize cosine distance and
  `ts_rank_cd` within the candidate pool, then combine with a fixed weight `w`). Rejected: min-max
  normalizing over a 20-candidate window means the meaning of a normalized score of, say, 0.8
  drifts from query to query, since it depends on whatever the other 19 candidates in that specific
  pool happened to score. A weight tuned against one query's score distribution is not the same
  weight for the next query's distribution, so `w` cannot be picked once and trusted; RRF has no
  such knob to mistune, only `RRF_K`, which the RRF literature already fixed.
- **A weighted blend with a learned weight**, fit against labelled relevance judgments. Rejected for
  this phase on principle, not just practicality: no labelled relevance data exists for this corpus,
  and the only candidate data with any relevance signal is the golden set. The golden set is the
  standard the whole system is measured against (ARCHITECTURE.md, "the golden set is hand-written by
  the user"); using it to fit a retrieval weight would mean tuning the system against its own eval,
  which is the exact anti-pattern CLAUDE.md's eval carve-out forbids. If a labelled relevance set is
  ever built specifically for retrieval tuning, separate from the golden set, this decision is worth
  revisiting.
