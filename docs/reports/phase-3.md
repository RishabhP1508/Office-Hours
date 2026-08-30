## Phase 3 Verification Report

Status: COMPLETE

Loop summary: 2 rounds.

- Round 1: the builder shipped the tsvector generated column, the GIN and HNSW indexes, the
  single-CTE RRF query, the config knobs, six tests, and the ADR. Independent verification found two
  defects. First, the semantic arm's inner `ORDER BY embedding <=> $1, id` made the HNSW index
  unreachable: an HNSW index scan can only satisfy an ORDER BY on the distance expression alone, so
  with a secondary sort key present the planner fell back to a sequential scan and a sort even with
  `enable_seqscan=off`. The index was created, valid, and permanently unused by the query that was
  supposed to use it, while `db.py`'s own comment claimed the arm was "index-backed (HNSW for
  distance)". Second, `test_keyword_only_chunk_is_retrievable` did not discriminate: on the 17-chunk
  fixture corpus the I-515A chunk is already at semantic rank 2 by hash accident, and the fixture
  corpus is smaller than the 20-deep candidate pool, so no chunk there can ever be keyword-only. The
  test passed with the keyword arm deleted.
- Round 2: the builder removed the inner tiebreak (the outer `ROW_NUMBER() OVER (ORDER BY distance,
  id)` already carries it, so rank determinism is unchanged), corrected the comment to state what
  each index actually does, and changed the test to assert the I-515A chunk ranks first, which fails
  both under semantic-only and under a mutation where the keyword rank is carried but never summed
  into the score. Verified that the tiebreak removal changes no current result. Two stale doc lines
  fixed in the same round.

No round weakened, skipped, or loosened a check. `eval/golden.jsonl`, `eval/run.py`'s `THRESHOLDS`,
`eval/baselines.json`, and every metric computation are byte-for-byte unchanged.

### Machine-checkable gate (ALL green for COMPLETE)

- [x] **DoD 1: init.sql creates the HNSW and GIN indexes** — ran:
  `docker exec office-hours-postgres-1 psql -U officehours -d officehours -c "\d documents"` — got:

      content_tsv | tsvector | | | generated always as (setweight(to_tsvector('english'::regconfig,
      section_heading), 'A'::"char") || setweight(to_tsvector('english'::regconfig, content),
      'B'::"char")) stored
      Indexes:
          "documents_pkey" PRIMARY KEY, btree (id)
          "documents_content_tsv_gin" gin (content_tsv)
          "documents_embedding_hnsw" hnsw (embedding vector_cosine_ops)

- [x] **The generated column backfilled without a re-ingest** — ran:
  `select count(*) as total, count(content_tsv) as with_tsv from documents;` — got: `216 | 216` —
  expected: 216 | 216. Applying `infra/sql/init.sql` to the already-ingested database IS the
  migration. `ADD COLUMN IF NOT EXISTS ... GENERATED ALWAYS AS (...) STORED` computes the value for
  every existing row at the moment the column is added. No re-embedding, no re-crawl, and the corpus
  the Phase 1 baseline ran against is bit-identical.

- [x] **DoD 2: the hybrid CTE returns results and the retrieve path uses it** — the CTE as shipped
  (`services/orchestrator/app/db.py`, `_HYBRID_SEARCH_SQL`):

      WITH q AS (
          SELECT replace(plainto_tsquery('english', %(query_text)s)::text, '&', '|')::tsquery AS query
      ),
      semantic AS (
          SELECT id, ROW_NUMBER() OVER (ORDER BY distance, id) AS rank
          FROM (
              SELECT id, embedding <=> %(embedding)s AS distance
              FROM documents
              ORDER BY embedding <=> %(embedding)s
              LIMIT %(candidate_pool)s
          ) s
      ),
      keyword AS (
          SELECT id, ROW_NUMBER() OVER (ORDER BY score DESC, id) AS rank
          FROM (
              SELECT d.id, ts_rank_cd(d.content_tsv, q.query) AS score
              FROM documents d, q
              WHERE d.content_tsv @@ q.query
              ORDER BY score DESC, d.id
              LIMIT %(candidate_pool)s
          ) kw
      ),
      fused AS (
          SELECT
              COALESCE(s.id, kw.id) AS id,
              COALESCE(1.0 / (%(rrf_k)s + s.rank), 0) + COALESCE(1.0 / (%(rrf_k)s + kw.rank), 0)
                  AS rrf_score,
              s.rank AS semantic_rank,
              kw.rank AS keyword_rank
          FROM semantic s FULL OUTER JOIN keyword kw ON s.id = kw.id
      )
      SELECT d.id, d.content, d.source_url, d.resolved_url, d.section_heading, d.heading_level,
             d.page_last_updated, d.fetched_at, d.last_verified_at,
             d.embedding <=> %(embedding)s AS distance,
             f.rrf_score, f.semantic_rank, f.keyword_rank
      FROM fused f JOIN documents d ON d.id = f.id
      ORDER BY f.rrf_score DESC, d.id
      LIMIT %(k)s

  `app/pipeline.py` calls `hybrid_search` and there is no other retrieval function left in
  `app/db.py`; Phase 0's `search()` is deleted. Confirmed the running container serves this code, not
  a stale image — ran: `docker exec office-hours-orchestrator-1 python -c "from app.db import
  _HYBRID_SEARCH_SQL ..."` — got the inner semantic `ORDER BY embedding <=> %(embedding)s` with
  `inner has id tiebreak: False`.

- [x] **Both indexes do what the code says they do** — ran `EXPLAIN (COSTS OFF)` on each arm as
  shipped, against the live 216-chunk corpus (vector literal elided):

      shipped semantic arm, default planner : Limit ; -> Sort ; Sort Key: ((embedding <=> <vec>)) ; -> Seq Scan on documents
      shipped semantic arm, seqscan off     : Limit ; -> Index Scan using documents_embedding_hnsw on documents
      shipped keyword arm,  default planner : Limit ; -> Sort ; Sort Key: (ts_rank_cd(...)) DESC, d.id
                                              ; -> Bitmap Heap Scan on documents d
                                              ; -> Bitmap Index Scan on documents_content_tsv_gin

  The GIN index is genuinely used today: the keyword arm's `@@` filter runs as a Bitmap Index Scan.
  The HNSW index is reachable but not currently firing, because at 216 rows the planner correctly
  prices an exact sequential scan below an index scan. That means the semantic arm is exact right
  now and the HNSW index is insurance for a larger corpus. Before round 2 it was not reachable at
  all. This is stated the same way in `db.py`'s comment rather than implying the index is doing work
  it is not.

- [x] **Removing the inner tiebreak changed no current result** — ran the shipped CTE against the
  live corpus with the real Ollama embedder, before and after the edit, for three golden questions.
  Got identical top-5 ids all three times, for example `[468, 444, 501, 505, 615]` for row 0. The
  eval numbers below therefore describe the code as it stands.

- [x] **DoD 6: CI-mode eval passes against the fixtures, in a dependency-matched environment** —
  the first attempt at this failed for an instructive reason and is recorded under Caveats. The
  passing run was inside a throwaway Linux container built the way the workflow builds its
  environment: `python:3.12-slim`, `pip install -e "./services/orchestrator[dev,eval-ci]"`, nothing
  else. Dependency probe first, because a run in a richer environment would prove nothing:

      python 3.12.14
      forbidden-but-installed: NONE          # ragas, langchain, langchain_core,
      openai: True textstat: True            # langchain_openai, langchain_community, datasets

  Then the workflow's own steps, against a fresh `officehours_ci_verify` database ingested only from
  `eval/fixtures/sources`. Ran: `python -m eval.run --ci` — got:

      applied infra/sql/init.sql
      TOTAL: 17 chunks across 4 sources
      citation_hallucination_rate   0.000   21/21   0.000   GATED (baseline)   PASS
      unreferenced_citation_rate    0.657 105/105   0.657   REPORTED             --
      reading_grade_level          12.620   21/21  12.620   REPORTED             --
      errored_rows                      0     n/a       0   GATED (baseline)   PASS
      empty_answer_rows                 0     n/a       0   GATED (baseline)   PASS
      non_advice_scored_count          15     n/a      15   GATED (baseline)   PASS
      advice_scored_count               6     n/a       6   GATED (baseline)   PASS
      unclassified_rows                 0     n/a       0   GATED (baseline)   PASS
      OVERALL: PASS
      EXIT_eval_run_ci=0

  **`eval/baselines.json` was not touched.** Every gated metric matches the recorded `ci_baseline`
  exactly, and so do both reported metrics. No baseline needed updating, so none was updated.

- [x] **Chunking invariants and guard tests still pass, in the same environment** — ran:
  `python -m pytest -m "not full_corpus" -q` — got: `46 passed, 6 deselected in 1.98s`,
  `EXIT_pytest=0`. That run includes
  `test_importing_eval_run_never_imports_ragas_or_langchain`, which is the test that matters here,
  and it is meaningful in this environment specifically because none of the four forbidden modules
  are installed in it.

- [x] **The six new hybrid tests actually run and are not silently deselected** — ran:
  `python -m pytest tests/test_hybrid_retrieval.py -v` — got 6 named PASSED lines, `6 passed`.

- [x] **The keyword test discriminates** — ran the semantic arm alone against the fixture database,
  with the same `StubEmbedder` vector the test uses — got:

      SEMANTIC-ONLY top5: 1. id=15 Meet with Your Designated School Official (DSO)
                          2. id=17 Form I-515A
      KEYWORD-ONLY top5:  1. id=17 Form I-515A  ts_rank_cd=5.2000   (sole tsquery match)

  Under semantic-only, id=15 leads. Under RRF, id=17 scores 1/62 + 1/61 = 0.03252 against id=15's
  1/61 = 0.01639, so id=17 leads. The test asserts `results[0]` is the I-515A chunk, so it fails
  under semantic-only and it also fails under a mutation that joins the keyword arm but drops its
  rank from the score.

- [x] **ruff and black** — ran: `ruff check app tests` — got: `All checks passed!`. Ran:
  `black --check app tests` — got: `All done! 18 files would be left unchanged.`

- [x] **The live corpus was never rebuilt or damaged** — ran: `select count(*) from documents;` —
  got: `216`, before the migration, after the migration, and after the eval run. All fixture
  ingestion went to separate databases.

- [x] **DoD 3: a full eval run on hybrid, compared against `20260830T183110Z`** — ran:
  `docker exec office-hours-orchestrator-1 python -m eval.run` — wrote
  `eval/results/20260830T230318Z.json`, 21/21 rows scored, 0 errored, 4126s wall clock, judged by
  `nvidia/nemotron-3.5-lightning-30b-a3b`. Numbers below.

- [x] **DoD 4: rows 0, 9, and 18 reported individually** — below.

- [x] **DoD 5: the RRF-versus-weighted ADR exists** — `docs/adr/0001-rrf-vs-weighted-blend.md`.

### Human-review items (the user confirms these)

- [ ] **Push, and the pull request's `ci-invariant-gate` run** — check: the Actions tab on the pull
      request for this branch. What you should see: `ci-invariant-gate` green, with
      `python -m eval.run --ci` printing `OVERALL: PASS` and pytest printing `46 passed, 6
      deselected`. This is human-review only because it depends on a commit and a push, which are
      yours by hand. What I can say is that the exact commands that job runs were run here in an
      environment with the same Python version and the same installed dependency set, and both
      passed.
- [ ] **Whether the quality movement is acceptable for this phase** — check: the two tables below.
      What you should see: retrieval clearly better, the non-advice subset better on every metric,
      and the advice subset worse on the model-scored metrics because the system now refuses more
      often. That last one is a judgment call about whether you accept the trade, and it is yours,
      not mine.
- [ ] **The RRF decision itself** — check: `docs/adr/0001-rrf-vs-weighted-blend.md`. What you should
      see: an argument you would be willing to defend out loud, including the part where RRF puts an
      irrelevant chunk above the correct one for row 0 and still counts as the right choice.

### Quality metrics (reported, NOT gated, never optimized against)

The full run gates on `THRESHOLDS` and came out `OVERALL: FAIL`, on `answer_relevancy`,
`advice_leakage_rate`, and `comprehensibility`. The Phase 1 baseline failed the same three plus
`false_refusal_rate`. Those thresholds are the Phase 4 target and nothing in this phase moved them.

Semantic-only baseline is `eval/results/20260830T183110Z.json`. Hybrid is
`eval/results/20260830T230318Z.json`. Neither run was repeated, and the baseline was not re-run,
because the corpus, the embedder, the generator, the prompt, and `RETRIEVAL_TOP_K=5` are all
unchanged; only the ranking function differs.

    metric                        semantic-only    hybrid RRF     delta     n before/after
    faithfulness                          0.910         0.919    +0.009            20/21
    answer_relevancy                      0.529         0.533    +0.005            20/21
    context_precision                     0.914         0.926    +0.012            21/21
    false_refusal_rate                    0.200         0.000    -0.200            15/15
    advice_leakage_rate                   0.833         0.500    -0.333              6/6
    comprehensibility                     2.905         2.857    -0.048            21/21
    citation_hallucination_rate           0.000         0.000     0.000            21/21
    unreferenced_citation_rate            0.733         0.657    -0.076          105/105
    reading_grade_level                  15.638        17.579    +1.941            21/21

**Read the faithfulness and answer_relevancy rows with care.** The baseline lost two scores to
transient errors (row 13's `answer_relevancy` to a judge 502, row 16's `faithfulness` to a parse
failure), and the hybrid run lost none, so those two averages are over different denominators.
Recomputed over only the 20 rows scored in both runs:

    metric                     common-n   like-for-like    published
    faithfulness                     20   0.910 -> 0.915   0.910 -> 0.919
    answer_relevancy                 20   0.529 -> 0.518   0.529 -> 0.533
    context_precision                21   0.914 -> 0.926   0.914 -> 0.926
    comprehensibility                21   2.905 -> 2.857   2.905 -> 2.857

So the honest statement about overall `answer_relevancy` is that it did not improve. It fell
slightly, by 0.011. The published +0.005 is an artifact of the baseline missing row 13, which the
hybrid run scored at 0.837.

#### context_precision split by is_advice, and the subset breakdowns

    subset                  n    faithfulness    ans_relevancy    ctx_precision    comprehens.
    is_advice=false        15   0.912 -> 0.978   0.594 -> 0.673   0.896 -> 0.910  2.933 -> 3.067
    is_advice=true          6   0.905 -> 0.772   0.377 -> 0.185   0.959 -> 0.967  2.833 -> 2.333
    volatility=stable      20   0.905 -> 0.915   0.513 -> 0.523   0.910 -> 0.922  2.900 -> 2.900
    volatility=volatile     1   1.000 -> 1.000   0.830 -> 0.738   1.000 -> 1.000  3.000 -> 2.000
    multi_part=false       18   0.894 -> 0.906   0.526 -> 0.532   0.900 -> 0.914  2.944 -> 3.000
    multi_part=true         3   1.000 -> 1.000   0.542 -> 0.542   1.000 -> 1.000  2.667 -> 2.000

Splitting by `is_advice` was the right call, and it changes the conclusion. On the 15 rows where the
system is supposed to answer, every metric improved: faithfulness +0.066, answer_relevancy +0.079,
context_precision +0.013, comprehensibility +0.133. On the 6 advice rows the model-scored metrics
fell, and the reason is visible in the per-row data: rows 6 and 8 flipped from ANSWER to REFUSAL, and
RAGAS scores a refusal 0.0 on `answer_relevancy` by design. `advice_leakage_rate` moving 0.833 to
0.500 is the same event measured from the other side. Refusing an advice question is the behavior
this project wants, so the advice subset getting worse on `answer_relevancy` is not straightforwardly
a regression. It is also six rows judged by a model, so it is weak evidence either way.

One caution about the aggregate that the brief anticipated, with a correction. The brief said
aggregate `context_precision` hides five rows scoring 0.0, all of them advice rows. The baseline
results file does not show that: no row scores 0.0 on `context_precision`, the minimum is 0.333 (rows
0 and 10), and advice rows score higher than non-advice, 0.959 against 0.896. The six 0.0 scores in
the baseline are on `answer_relevancy` (rows 0, 5, 7, 8, 9, 18), which is where RAGAS's noncommittal
detector puts a refusal. The instinct behind the caution held, though: the advice subset's
`context_precision` was already at 0.959 and had nowhere to go, and it moved 0.008.

#### Retrieval measured on its own

The eval cannot separate retrieval from generation, so this was measured directly, with no LLM and no
judge in the loop: for each golden row, does any of its hand-written `source_urls` appear among the
top-5 retrieved chunks? Measured over the live corpus, with the shipped CTE and with Phase 0's
semantic-only query.

    golden-source hit rate in top-5     semantic-only    hybrid RRF
    all 21 rows                                 0.905         1.000
    non-advice (15)                             0.867         1.000
    advice (6)                                  1.000         1.000

Two rows changed, both `miss -> HIT`: rows 0 and 9. No row regressed. The advice subset was already
at ceiling. This measures retrieval; it did not tune anything, and the golden set was not used to
select `RRF_K`, `HYBRID_CANDIDATE_POOL`, or the tsquery construction.

#### Rows 0, 9, and 18, individually

    row   context_precision   faithfulness   answer_relevancy   comprehensibility   refusal
    0     0.333 -> 0.450      1.000 -> 1.000   0.000 -> 0.843     1 -> 4            REFUSAL -> ANSWER
    9     0.887 -> 1.000      0.500 -> 1.000   0.000 -> 0.826     2 -> 3            ANSWER  -> ANSWER
    18    1.000 -> 1.000      1.000 -> 1.000   0.000 -> 0.000     1 -> 1            REFUSAL -> ANSWER

**Row 0, "What is the I-983 and who fills it out?" is fixed.** Before, the answer was: "The provided
sources do not contain any information about Form I-983 or who completes that form. The context
discusses H-1B petitions (Form I-129), electronic registration processes, wage levels, SOC codes, and
a specific $100,000 payment requirement." After: "The Form I-983 is described as a 'Training Plan for
STEM OPT Students' that an employer must complete when employing a student participating in the STEM
OPT extension program [2]. An employer ... fills out and signs this form." Citation [2] is chunk 444,
the STEM OPT chunk that names the form. Semantic-only retrieved five H-1B chunks and none of the
right page; hybrid retrieves `[468, 444, 501, 505, 615]`. `context_precision` only reached 0.450
because three of the five chunks are still the semantic arm's H-1B results, which is the RRF
behavior the ADR describes rather than a bug.

**Row 9, "How long is post-completion OPT?" is fixed.** Before: "Your sources do not provide a
specific total length for post-completion OPT (e.g., 12 months or 364 days)." After: "Post-completion
OPT employment authorization cannot end more than 12 months." This had failed the same way across two
consecutive baseline runs, so it was systematic, and hybrid resolved it. Faithfulness doubled, 0.5 to
1.0, and `context_precision` reached 1.0.

**Row 18, the pre-completion deduction plus STEM extension, is not fixed, and hybrid was never going
to fix it.** `context_precision` was 1.0 before and is 1.0 after: retrieval delivered everything
needed both times. The answer still declines to do the arithmetic: "Your sources do not cover the
specific calculation for using exactly three months of full-time pre-completion OPT and what remains
available." `answer_relevancy` is still 0.0 and comprehensibility is still 1. This is a
fact-combination failure in generation, and it belongs to Phase 4. The judge did reclassify the row
from REFUSAL to ANSWER, but the text is still a non-answer, so treat that flip as judge noise rather
than progress.

### How the core piece works (plain English)

One SQL query runs two searches over the same table and merges them by position rather than by score.
The semantic arm sorts chunks by cosine distance between the question's embedding and each chunk's
embedding, and takes the closest 20. The keyword arm turns the question into a Postgres text query,
matches it against a stored `tsvector` of each chunk's heading and body, ranks by `ts_rank_cd`, and
takes the top 20. Each arm's results are numbered 1, 2, 3 and so on. Every chunk that either arm
found then gets a fused score of `1/(60 + its rank in the first arm)` plus `1/(60 + its rank in the
second arm)`, counting a missing rank as zero, and the five highest go to the generator. Merging by
rank is the whole point: cosine distance and `ts_rank_cd` are not on a common scale, so adding them
directly would be meaningless and normalizing them would need a weight that drifts from query to
query. Ranks are just small integers, so they compare across arms without any normalization. The one
non-obvious detail is that the keyword arm ORs the question's terms instead of ANDing them.
`websearch_to_tsquery` on "What is the I-983 and who fills it out?" produces `'-983' & 'fill'`, which
matches none of the 216 chunks, because the chunk that names the I-983 never says "fill". OR-ed, that
chunk ranks first in the keyword arm and RRF carries it into the top five, which is exactly the
failure this phase set out to fix.

### Decisions logged

- `docs/adr/0001-rrf-vs-weighted-blend.md` — fuse by rank with RRF, not by a weighted blend of
  normalized scores, because cosine distance and `ts_rank_cd` share no scale and a min-max weight
  fitted inside a 20-candidate window means something different for every query. Records the cost
  (RRF discards margin, so a runaway best semantic match scores the same as a marginal rank 1) and
  rejects a learned weight on principle, since the only data with a relevance signal is the golden
  set and fitting retrieval to it would tune the system against its own eval.

### Caveats / not done

- **The full run is `OVERALL: FAIL` against `THRESHOLDS`**, on `answer_relevancy` (0.533 against
  0.75), `advice_leakage_rate` (0.500 against 0.10), and `comprehensibility` (2.857 against 3.5).
  The baseline failed those three and `false_refusal_rate` as well. These are Phase 4 targets, they
  are not this phase's gate, and nothing here was tuned toward them.
- **Overall `answer_relevancy` did not improve.** On a like-for-like 20 rows it fell 0.011. The
  improvement is confined to the non-advice subset (+0.067 on the common rows).
- **Four rows lost `context_precision`:** row 14 (H-1B registration cost) 1.000 to 0.639, row 15
  (registration window) 1.000 to 0.750, row 2 (which form for OPT) 1.000 to 0.804, row 7 0.950 to
  0.917. This is the OR-ed keyword arm's cost: it admits chunks that share a common word with the
  question. Row 15 is the clearest regression overall, with `answer_relevancy` 0.846 to 0.000 and
  comprehensibility 4 to 2. Against that, row 10 went 0.333 to 1.000 and rows 9 and 17 went 0.887 to
  1.000. Net aggregate is +0.012, and the aggregate is hiding movement in both directions.
- **`reading_grade_level` got worse, 15.638 to 17.579.** Not gated, and not addressed here.
  Plain-language shaping is Phase 4's job.
- **Judge variance is not quantified.** `comprehensibility` and the refusal classification are
  single calls to a hosted model. The determinism check re-scored row 0 twice and got 4 both times,
  but that is one row. Several per-row swings in the tables above are within what judge noise could
  produce, especially in the six-row advice subset. Do not read individual row deltas as precise.
- **The first attempt at the dependency-matched CI verification failed, and the reason is worth
  keeping.** A Windows-host Python 3.12 virtualenv with `[dev,eval-ci]` installed had exactly the
  right dependency set, and still could not run the check: `psycopg` async raises
  `InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode` on Windows, so
  `app.ingest` ingested nothing and the orchestrator's pool timed out. Matching the dependency set
  is necessary but not sufficient; the environment has to be able to run the thing. The passing run
  used a Linux container, which is what the workflow uses. Reproducing it needs Docker, not just a
  virtualenv.
- **CI mode cannot tell you whether hybrid retrieval works.** Its reported metrics came out
  identical to the recorded baseline to three decimals, including `reading_grade_level` at 12.620.
  That is expected: `StubLLM`'s answer text depends on how many contexts it is given, not on what
  they say, so the CI numbers are insensitive to which chunks retrieval returns. CI passing here is
  evidence that the change did not break retrieval plumbing, citation mapping, or error handling. It
  is not evidence that the ranking improved.
- **HNSW is not currently doing any work.** At 216 rows the planner prices an exact sequential scan
  below an index scan, so the semantic arm is exact. The index is reachable now (verified with
  `enable_seqscan=off`) and will start being used as the corpus grows, at which point the semantic
  arm becomes approximate. Worth remembering when the corpus expands, because retrieval results can
  shift for that reason alone.
- **Leftover artifacts from verification, none of them in the repo layout.** A throwaway container
  `oh-ci-verify` and a database `officehours_ci_verify` hold the reproducible CI check; the builder
  left a database `officehours_fixtures` for local test runs. All three are disposable
  (`docker rm -f oh-ci-verify`, `dropdb officehours_ci_verify officehours_fixtures`). They were left
  in place so the verification can be re-run. `services/orchestrator/office_hours_orchestrator.egg-info`
  is gitignored.
- **Two empty directories predate this phase and are not mine to delete:** `eval;C` and
  `infra/sql/init.sql;C`, left by a shell redirect in an earlier session. Git does not track empty
  directories so they will not be committed, but they will confuse anyone who lists the tree.
- **Nothing was committed, pushed, branched, or opened as a pull request.** All of that is yours.
