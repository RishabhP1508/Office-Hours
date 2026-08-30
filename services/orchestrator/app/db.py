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
    fetched_at: datetime
    last_verified_at: datetime
    distance: float
    rrf_score: float
    semantic_rank: int | None
    keyword_rank: int | None


async def _configure(conn) -> None:
    await register_vector_async(conn)


def make_pool(database_url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(database_url, open=False, configure=_configure)


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
#
# `distance` is recomputed in the final SELECT (not just carried from the semantic CTE) so a
# keyword-only hit still carries a real cosine distance, and RetrievedChunk.distance keeps the same
# meaning it had in Phase 0.
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
)
SELECT d.id, d.content, d.source_url, d.resolved_url, d.section_heading, d.heading_level,
       d.page_last_updated, d.fetched_at, d.last_verified_at,
       d.embedding <=> %(embedding)s AS distance,
       f.rrf_score, f.semantic_rank, f.keyword_rank
FROM fused f JOIN documents d ON d.id = f.id
ORDER BY f.rrf_score DESC, d.id
LIMIT %(k)s
"""


async def hybrid_search(
    pool: AsyncConnectionPool,
    query_embedding: list[float],
    query_text: str,
    k: int,
    *,
    rrf_k: int,
    candidate_pool: int,
) -> list[RetrievedChunk]:
    """Return the k chunks with the highest RRF score, fused from a semantic and a keyword arm.

    `candidate_pool` is how deep each arm looks before ranks are fused (see
    Settings.HYBRID_CANDIDATE_POOL); it is independent of `k`, the number of chunks returned.
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
                },
            )
            rows = await cur.fetchall()
    return [RetrievedChunk(**row) for row in rows]
