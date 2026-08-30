"""Tests for app.db.hybrid_search: semantic + keyword fusion with Reciprocal Rank Fusion.

Every DB-backed test in this file runs against whatever DATABASE_URL points at -- in the CI
invariant gate (.github/workflows/eval.yml) that is the ephemeral postgres service container,
ingested from eval/fixtures/sources via INGEST_MODE=snapshot (4 files, 17 chunks). Locally it was
run against a separate `officehours_fixtures` database in the same postgres instance as the live
216-chunk `officehours` database, ingested the same way with EMBED_PROVIDER=stub, specifically so
these tests never touch the live corpus. See docs/reports/phase-3.md for the exact commands.

The stub embedder (app.providers.embeddings.StubEmbedder) is a deterministic hash, not a semantic
embedding -- it carries no notion of which texts are "similar" (see its own docstring). On the
17-chunk fixture corpus, with HYBRID_CANDIDATE_POOL=20 (bigger than the whole corpus), every chunk
always lands in the semantic arm's candidate pool -- there is no such thing as a chunk the semantic
arm never sees here, only chunks it ranks well or badly by hash accident. So
test_keyword_only_chunk_is_retrievable below does not (and cannot, on this corpus) test "a chunk
with no semantic signal"; what it tests is that the keyword arm's rank-1 match for a distinctive
term actually moves the fused rrf_score enough to put that chunk ahead of whatever the hash-based
semantic arm happens to rank first, which requires the keyword rank to be real and to be summed
into the score, not just carried as a spare column.
"""

import os

import pytest

from app.config import Settings, get_settings
from app.db import hybrid_search, make_pool
from app.providers.embeddings import StubEmbedder

# pyproject.toml sets asyncio_mode = "auto", so `async def test_*` functions below run as asyncio
# tests without an explicit marker; adding one anyway would also mis-mark the one plain (sync)
# test in this file, test_retrieval_top_k_defaults_to_five.

# The one fixture chunk (eval/fixtures/sources/travel-studyinthestates-traveling-f-or-m.md,
# "## Form I-515A") that contains this literal string; verified with
# `grep -rn "I-515A" eval/fixtures/sources` to appear in no other fixture file.
_KEYWORD_ONLY_TERM = "I-515A"
_KEYWORD_ONLY_HEADING = "Form I-515A"


@pytest.fixture
async def pool():
    settings = get_settings()
    database_url = os.environ.get("DATABASE_URL", settings.DATABASE_URL)
    p = make_pool(database_url)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def embedder():
    return StubEmbedder(dim=768)


async def test_keyword_only_chunk_is_retrievable(pool, embedder):
    """The "Form I-515A" chunk is the sole keyword match for a query containing "I-515A"
    (keyword_rank 1). On this 17-chunk corpus with HYBRID_CANDIDATE_POOL=20, it is also present in
    the semantic arm (every chunk is, since the pool is bigger than the whole corpus) -- but only
    at semantic_rank 2, because the stub embedder's hash puts a different chunk ("Meet with Your
    Designated School Official (DSO)") at semantic_rank 1 for this exact query text by accident,
    not by relevance.

    Semantic rank alone would put that other chunk first: 1/(60+1) alone beats nothing. Fused with
    the keyword arm, "Form I-515A" scores 1/(60+2) + 1/(60+1), which outranks the other chunk's
    semantic-only 1/(60+1). So this only passes if the keyword rank is both real (the keyword arm
    actually found the chunk) and actually added into rrf_score, not just carried as an unused
    column on the row -- either failure mode leaves the other chunk in first place instead.
    """
    question = f"What is a {_KEYWORD_ONLY_TERM} and when is it issued?"
    [embedding] = await embedder.embed([question])

    results = await hybrid_search(pool, embedding, question, k=5, rrf_k=60, candidate_pool=20)

    assert results, "Expected at least one result"
    assert results[0].section_heading == _KEYWORD_ONLY_HEADING, (
        f"Expected the {_KEYWORD_ONLY_HEADING!r} chunk to rank FIRST for a query containing "
        f"{_KEYWORD_ONLY_TERM!r} -- its keyword_rank=1 contribution should outrank whatever the "
        "hash-based semantic arm ranks first on its own -- got (heading, semantic_rank, "
        f"keyword_rank) in order: "
        f"{[(r.section_heading, r.semantic_rank, r.keyword_rank) for r in results]}"
    )
    assert results[0].keyword_rank is not None, (
        "The top-ranked chunk carries no keyword_rank -- the keyword arm did not actually "
        "contribute it."
    )


async def test_rrf_ordering_is_by_rrf_score_descending_with_at_least_one_rank(pool, embedder):
    question = "How long is the STEM OPT extension and what happens with cap-gap?"
    [embedding] = await embedder.embed([question])

    results = await hybrid_search(pool, embedding, question, k=5, rrf_k=60, candidate_pool=20)

    assert results, "Expected at least one result"
    scores = [r.rrf_score for r in results]
    assert scores == sorted(
        scores, reverse=True
    ), f"Results are not sorted by rrf_score descending: {scores}"
    for r in results:
        assert r.semantic_rank is not None or r.keyword_rank is not None, (
            f"Chunk {r.id} has neither a semantic_rank nor a keyword_rank -- it should not have "
            "been a fusion candidate at all"
        )


async def test_stopword_only_query_degrades_to_semantic_only(pool, embedder):
    """An all-stopword query text produces an empty tsquery: the keyword arm matches nothing, and
    RRF must degrade cleanly to the semantic arm alone rather than returning fewer than k results.
    """
    question = "is the a of"
    [embedding] = await embedder.embed([question])

    results = await hybrid_search(pool, embedding, question, k=5, rrf_k=60, candidate_pool=20)

    assert len(results) == 5, f"Expected 5 chunks from the semantic arm alone, got {len(results)}"
    for r in results:
        assert r.keyword_rank is None, (
            f"Chunk {r.id} has a keyword_rank ({r.keyword_rank}) for an all-stopword query, "
            "which should match nothing in the keyword arm"
        )
        assert r.semantic_rank is not None


async def test_hybrid_search_is_deterministic(pool, embedder):
    question = "What documents do I need at a U.S. port of entry as an F-1 student?"
    [embedding] = await embedder.embed([question])

    first = await hybrid_search(pool, embedding, question, k=5, rrf_k=60, candidate_pool=20)
    second = await hybrid_search(pool, embedding, question, k=5, rrf_k=60, candidate_pool=20)

    assert [r.id for r in first] == [
        r.id for r in second
    ], "The same query returned different ids/order on a second call"


async def test_documents_table_has_hnsw_and_gin_indexes(pool):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT indexdef FROM pg_indexes WHERE tablename = 'documents'")
            rows = await cur.fetchall()
    indexdefs = [row[0] for row in rows]
    assert any(
        "USING hnsw" in d for d in indexdefs
    ), f"No index on documents uses the hnsw access method: {indexdefs}"
    assert any(
        "USING gin" in d for d in indexdefs
    ), f"No index on documents uses the gin access method: {indexdefs}"


def test_retrieval_top_k_defaults_to_five():
    """Guards the Phase 1 semantic-only baseline comparison: RETRIEVAL_TOP_K must not silently
    change, or the number of chunks the generator sees would no longer match the baseline run
    (eval/results/20260830T183110Z.json) it is being compared against.
    """
    assert Settings().RETRIEVAL_TOP_K == 5
