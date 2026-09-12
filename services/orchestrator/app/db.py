"""Postgres access: a connection pool and the retrieval query.

Phase 3 retrieval is hybrid: a semantic arm (pgvector cosine distance, backed by the HNSW index)
and a keyword arm (a tsvector column, backed by a GIN index), fused with Reciprocal Rank Fusion in
a single CTE (see ARCHITECTURE.md, "Hybrid search fuses semantic and keyword ranks with RRF in one
CTE", and docs/adr/0001-rrf-vs-weighted-blend.md). Phase 0's plain, index-free cosine scan is gone;
`hybrid_search` is now the only retrieval path pipeline.py calls.
"""

from dataclasses import dataclass
from datetime import date, datetime

from pgvector import Vector
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


@dataclass
class RetrievedChunk:
    id: int
    content: str
    source_url: str
    resolved_url: str | None
    section_heading: str
    heading_level: int
    page_last_updated: date | None
    rule_effective_date: date | None
    fetched_at: datetime
    last_verified_at: datetime
    distance: float
    rrf_score: float
    semantic_rank: int | None
    keyword_rank: int | None
    # "fusion" for every chunk the RRF fusion above actually ranked into the top k;
    # "dated_companion" for a chunk added afterward solely because it carries the same
    # rule_effective_date as a chunk already in that top k (see the `companions` CTE below and
    # Settings.DATED_RULE_COMPANIONS). A companion is admitted because a dated rule is in play,
    # not because it is relevant, so anything that gates on relevance (app/pipeline.py's
    # no-answer check) must filter on this field.
    retrieved_by: str


async def _configure(conn) -> None:
    await register_vector_async(conn)


def make_pool(database_url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        database_url,
        open=False,
        configure=_configure,
        # Neon's pooler drops idle server connections, so a pooled connection can be dead by the time
        # it is handed out. check runs a cheap liveness probe first and discards a dead one; max_idle
        # recycles below the pooler's own idle timeout so it usually never comes to that.
        check=AsyncConnectionPool.check_connection,
        max_idle=180.0,
        max_lifetime=1800.0,
    )


# Single-CTE hybrid retrieval: a semantic arm and a keyword arm, each ranked independently, fused
# by Reciprocal Rank Fusion. Three decisions worth recording inline, because each is easy to get
# wrong on a re-read and each was verified against the live 216-chunk corpus:
#
# 1. plainto_tsquery, then '&' -> '|': the keyword arm must OR the query's terms, not AND them.
#    websearch_to_tsquery('english', 'What is the I-983 and who fills it out?') yields
#    '-983' & 'fill', which matches ZERO of the 216 chunks even though chunk 444 is the STEM OPT
#    I-983 chunk, because that chunk's text never contains the word "fill". OR-ed instead
#    ('-983' | 'fill'), chunk 444 ranks first (ts_rank_cd 3.6) against 0.8 for the runner-up.
#    plainto_tsquery specifically, not websearch_to_tsquery: plainto_tsquery never emits `!`
#    negation or `|` itself, so the string replace of '&' -> '|' is total and safe --
#    plainto_tsquery('english', 'opt -stem') is 'opt' & 'stem' (the leading "-" is just punctuation
#    to plainto_tsquery, not a negation operator), verified against the live corpus. An
#    all-stopword query ("is the a of") produces an empty tsquery; `@@` against an empty tsquery
#    matches nothing, so the keyword arm returns no rows and RRF degenerates cleanly to
#    semantic-only.
# 2. Ranks come from ROW_NUMBER() over an already-LIMITed inner subquery, and the two arms are NOT
#    symmetric in how that inner LIMIT is satisfied. The keyword arm's GIN index backs the `@@`
#    filter (a Bitmap Index Scan picks the rows that match the tsquery at all); it cannot provide
#    ts_rank_cd order, so ranking those matches is always a sort, however few of them there are.
#    The semantic arm's inner ORDER BY deliberately has NO secondary sort key: `ORDER BY embedding
#    <=> %(embedding)s` alone is the one shape of ORDER BY an HNSW index scan can satisfy.
#    Verified with EXPLAIN (COSTS OFF) on the live 216-chunk corpus: adding `, id` as a tiebreak
#    here makes the HNSW index permanently unreachable for this query, even with sequential scans
#    forcibly disabled, because an ORDER BY on (distance, id) is not an ORDER BY the index can
#    produce -- so the id tiebreak has to live only in the outer ROW_NUMBER() below, where it costs
#    nothing (ROW_NUMBER's OVER clause is always a sort regardless of index). At 216 rows the
#    planner still picks a sequential scan over the HNSW index anyway, on cost -- an exact
#    sequential scan across 216 rows is cheaper than an approximate index lookup, so the semantic
#    arm is exact today, not approximate. The HNSW index is insurance for when the corpus is large
#    enough that the planner's cost estimate flips, not an optimization firing right now; it is not
#    doing any work yet, and this file should not imply otherwise. The keyword arm's `, d.id`
#    tiebreak is unaffected by any of this (GIN can never provide the ts_rank_cd sort either way),
#    so it stays exactly as it was.
# 3. FULL OUTER JOIN, not UNION or INNER JOIN: a chunk found by only one arm must still be a
#    fusion candidate. That is the entire reason chunk 444 (I-983, keyword_rank 1, semantic_rank
#    NULL) is retrievable at all -- an INNER JOIN would drop it, and a UNION would double-count a
#    chunk found by both arms instead of combining its two ranks into one score.
# 4. `top` and `companions` (dated-rule companion retrieval, docs/adr/0019-dated-rule-companion-
#    retrieval.md) sit AFTER `fused` and touch neither arm nor the FULL OUTER JOIN above. `top` is
#    exactly the `ORDER BY rrf_score DESC, id LIMIT k` the final SELECT used to apply directly
#    against `fused` before this existed -- freezing it into its own CTE changes nothing about
#    ranking, it only gives `companions` a concrete top-k to compare against. `companions` finds up
#    to %(companions)s more chunks that (a) carry a rule_effective_date, (b) carry the SAME
#    rule_effective_date as some chunk already in `top`, and (c) are not already in `top`, ordered
#    by raw cosine distance to the query rather than by any fused rank -- a companion is admitted
#    because a dated rule is already in play, not because it ranked. When %(companions)s is 0
#    (Settings.DATED_RULE_COMPANIONS's off switch) or no chunk in `top` carries a
#    rule_effective_date at all, `companions` returns zero rows (a LIMIT 0 short-circuits
#    regardless of the WHERE clause, and an empty date set makes the WHERE clause false for every
#    row either way), and the final SELECT's UNION ALL below degrades to exactly what it read
#    before `companions` existed. A companion chunk carries its real rrf_score/semantic_rank/
#    keyword_rank via a LEFT JOIN back onto `fused` when it happened to also be a fusion candidate
#    ranked outside the top-k cut, and 0/NULL when it was never a fusion candidate at all --
#    `retrieved_by` ('fusion' vs 'dated_companion') is what lets anything downstream tell the two
#    apart, which matters because app/pipeline.py's no-answer gate must ignore companions entirely.
#
# `distance` is recomputed in the final SELECT (not just carried from the semantic CTE, or from
# `companions`' own ORDER BY) so every returned row carries a real cosine distance computed the same
# way, and RetrievedChunk.distance keeps the same meaning it had in Phase 0.
#
# Final-SELECT ordering: all `top` (fusion) rows first, in today's `rrf_score DESC, id` order, then
# all `companions` rows, in ascending distance -- this is what determines citation numbering, so it
# has to hold exactly regardless of how a companion's own (possibly real) rrf_score compares to a
# fusion row's. `group_order` (0 for fusion, 1 for companion) is the primary sort key so the two
# groups never interleave; `group_rrf`/`group_id` are populated only for fusion rows (NULL for
# companions, which must never be reordered by an incidental leftover fusion score) and
# `group_distance` only for companion rows (NULL for fusion rows, which must never be reordered by
# distance) -- each pair of helper columns is inert outside the group it belongs to.
_HYBRID_SEARCH_SQL = """
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
),
top AS (
    SELECT id, rrf_score, semantic_rank, keyword_rank
    FROM fused
    ORDER BY rrf_score DESC, id
    LIMIT %(k)s
),
companions AS (
    SELECT d.id, d.embedding <=> %(embedding)s AS distance
    FROM documents d
    WHERE d.rule_effective_date IS NOT NULL
      AND d.rule_effective_date IN (
          SELECT dt.rule_effective_date
          FROM documents dt
          JOIN top t ON t.id = dt.id
          WHERE dt.rule_effective_date IS NOT NULL
      )
      AND d.id NOT IN (SELECT id FROM top)
    ORDER BY d.embedding <=> %(embedding)s ASC
    LIMIT %(companions)s
)
-- Phase 7: resolved_url/page_last_updated/fetched_at/last_verified_at moved off `documents` onto
-- `sources` (one row per source instead of one identical copy per chunk -- see infra/sql/init.sql).
-- This join pattern is unchanged by the dated-companion addition above -- it is simply now used in
-- both arms of the UNION ALL below instead of once -- so RRF's ranking behavior is unaffected.
-- Every distinct source_url in `documents` has a matching row in `sources` (the FK enforces it), so
-- this is always effectively an inner join in practice -- but it stays a plain JOIN, not LEFT JOIN,
-- precisely because the FK makes "no matching sources row" a schema violation, not a real case this
-- query needs to tolerate.
SELECT id, content, source_url, resolved_url, section_heading, heading_level, page_last_updated,
       rule_effective_date, fetched_at, last_verified_at, distance, rrf_score, semantic_rank,
       keyword_rank, retrieved_by
FROM (
    SELECT
        d.id, d.content, d.source_url, s.resolved_url, d.section_heading, d.heading_level,
        s.page_last_updated, d.rule_effective_date, s.fetched_at, s.last_verified_at,
        d.embedding <=> %(embedding)s AS distance,
        t.rrf_score, t.semantic_rank, t.keyword_rank,
        'fusion' AS retrieved_by,
        0 AS group_order, t.rrf_score AS group_rrf, d.id AS group_id, NULL::float8 AS group_distance
    FROM top t
    JOIN documents d ON d.id = t.id
    JOIN sources s ON s.source_url = d.source_url

    UNION ALL

    SELECT
        d.id, d.content, d.source_url, s.resolved_url, d.section_heading, d.heading_level,
        s.page_last_updated, d.rule_effective_date, s.fetched_at, s.last_verified_at,
        d.embedding <=> %(embedding)s AS distance,
        COALESCE(f.rrf_score, 0) AS rrf_score, f.semantic_rank, f.keyword_rank,
        'dated_companion' AS retrieved_by,
        1 AS group_order, NULL::float8 AS group_rrf, NULL::bigint AS group_id,
        c.distance AS group_distance
    FROM companions c
    JOIN documents d ON d.id = c.id
    JOIN sources s ON s.source_url = d.source_url
    LEFT JOIN fused f ON f.id = c.id
) combined
ORDER BY group_order ASC, group_rrf DESC, group_id ASC, group_distance ASC
"""


async def hybrid_search(
    pool: AsyncConnectionPool,
    query_embedding: list[float],
    query_text: str,
    k: int,
    *,
    rrf_k: int,
    candidate_pool: int,
    dated_rule_companions: int,
) -> list[RetrievedChunk]:
    """Return the k chunks with the highest RRF score, fused from a semantic and a keyword arm,
    plus (when `dated_rule_companions` > 0 and the fused top-k already contains a chunk carrying a
    rule_effective_date) up to `dated_rule_companions` more chunks carrying that same date, ordered
    by raw cosine distance -- see the `top`/`companions` CTEs and the long comment above
    _HYBRID_SEARCH_SQL for exactly how (docs/adr/0019-dated-rule-companion-retrieval.md).

    `candidate_pool` is how deep each arm looks before ranks are fused (see
    Settings.HYBRID_CANDIDATE_POOL); it is independent of `k`, the number of chunks returned.

    `dated_rule_companions` has no default -- every caller (app/pipeline.py passes
    Settings.DATED_RULE_COMPANIONS; a test wanting the pre-companion behavior passes 0 explicitly)
    states its own intent rather than silently inheriting one.

    Returned order: every fusion row first (today's `rrf_score DESC, id`), then every companion row
    (ascending distance) -- see RetrievedChunk.retrieved_by to tell them apart. This order is what
    determines citation numbering.
    """
    vector = Vector(query_embedding)
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _HYBRID_SEARCH_SQL,
                {
                    "embedding": vector,
                    "query_text": query_text,
                    "rrf_k": rrf_k,
                    "candidate_pool": candidate_pool,
                    "k": k,
                    "companions": dated_rule_companions,
                },
            )
            rows = await cur.fetchall()
    return [RetrievedChunk(**row) for row in rows]
