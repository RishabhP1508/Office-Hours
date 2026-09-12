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
import re
from datetime import UTC, date, datetime

import pytest

from app.config import Settings, get_settings
from app.db import RetrievedChunk, hybrid_search, make_pool
from app.pipeline import answer_question
from app.providers.embeddings import OllamaEmbedder, StubEmbedder
from app.providers.llm import LLM
from app.schemas import ResponseType

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

    results = await hybrid_search(
        pool, embedding, question, k=5, rrf_k=60, candidate_pool=20, dated_rule_companions=0
    )

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

    results = await hybrid_search(
        pool, embedding, question, k=5, rrf_k=60, candidate_pool=20, dated_rule_companions=0
    )

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

    results = await hybrid_search(
        pool, embedding, question, k=5, rrf_k=60, candidate_pool=20, dated_rule_companions=0
    )

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

    first = await hybrid_search(
        pool, embedding, question, k=5, rrf_k=60, candidate_pool=20, dated_rule_companions=0
    )
    second = await hybrid_search(
        pool, embedding, question, k=5, rrf_k=60, candidate_pool=20, dated_rule_companions=0
    )

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


# =================================================================================================
# Dated-rule companion retrieval (docs/adr/0019-dated-rule-companion-retrieval.md). Every test
# below that queries the live corpus is `full_corpus`-marked and uses the real OllamaEmbedder --
# the dated fixed_admission content (rule_effective_date=2026-09-15) these tests depend on exists
# only in the real 14-source manifest's ingested corpus, never in eval/fixtures/sources/.
# =================================================================================================

# Confirmed against the live corpus: the fused top-k for this question carries no
# rule_effective_date at all, so it is the "companions must add nothing" control below.
_NO_DATED_CHUNK_QUERY = "How long is my travel signature valid on my I-20?"

# The live motivating case (Settings.DATED_RULE_COMPANIONS's own comment): the fused top-k already
# contains A dated chunk, but not the one that actually states the new 30-day departure number.
_HAS_DATED_CHUNK_QUERY = "What is the grace period after OPT ends?"


@pytest.fixture
def real_embedder():
    settings = get_settings()
    return OllamaEmbedder(base_url=settings.OLLAMA_BASE_URL, model=settings.EMBED_MODEL)


@pytest.mark.full_corpus
async def test_dated_rule_companions_off_matches_pre_companion_behavior(pool, real_embedder):
    """dated_rule_companions=0 must return exactly k rows, every one `retrieved_by == "fusion"`,
    reproducing hybrid_search's behavior from before the `companions` CTE existed. Proved by
    comparing it against a companions-enabled call on a query whose fused top-k contains no dated
    chunk to add (_NO_DATED_CHUNK_QUERY, confirmed above) -- if the two calls' id lists match in
    order, dated_rule_companions=0 has changed nothing about the underlying fusion query.
    """
    settings = get_settings()
    [embedding] = await real_embedder.embed([_NO_DATED_CHUNK_QUERY])

    off = await hybrid_search(
        pool,
        embedding,
        _NO_DATED_CHUNK_QUERY,
        k=settings.RETRIEVAL_TOP_K,
        rrf_k=settings.RRF_K,
        candidate_pool=settings.HYBRID_CANDIDATE_POOL,
        dated_rule_companions=0,
    )
    on = await hybrid_search(
        pool,
        embedding,
        _NO_DATED_CHUNK_QUERY,
        k=settings.RETRIEVAL_TOP_K,
        rrf_k=settings.RRF_K,
        candidate_pool=settings.HYBRID_CANDIDATE_POOL,
        dated_rule_companions=2,
    )

    assert len(off) == settings.RETRIEVAL_TOP_K, f"expected exactly k rows, got {len(off)}"
    assert all(
        r.retrieved_by == "fusion" for r in off
    ), f"expected every row to be retrieved_by='fusion': {[(r.id, r.retrieved_by) for r in off]}"
    off_ids = [r.id for r in off]
    on_ids = [r.id for r in on]
    assert off_ids == on_ids, (
        "dated_rule_companions=0 must return the identical id list, in the identical order, as a "
        f"companions-enabled call on a query with no dated chunk to add -- off={off_ids} "
        f"on={on_ids}"
    )


@pytest.mark.full_corpus
async def test_no_dated_chunk_in_top_k_adds_no_companions(pool, real_embedder):
    """A query whose fused top-k contains no chunk carrying a rule_effective_date must return zero
    companions even with dated_rule_companions=2 -- the companions CTE's WHERE clause requires a
    date already present in `top` to match against, and an empty set of dates makes that clause
    false for every row in `documents`, regardless of the LIMIT.
    """
    settings = get_settings()
    [embedding] = await real_embedder.embed([_NO_DATED_CHUNK_QUERY])

    results = await hybrid_search(
        pool,
        embedding,
        _NO_DATED_CHUNK_QUERY,
        k=settings.RETRIEVAL_TOP_K,
        rrf_k=settings.RRF_K,
        candidate_pool=settings.HYBRID_CANDIDATE_POOL,
        dated_rule_companions=2,
    )

    assert not any(r.rule_effective_date is not None for r in results), (
        f"expected no dated chunk in the fused top-k for {_NO_DATED_CHUNK_QUERY!r}, got "
        f"{[(r.id, r.rule_effective_date) for r in results]}"
    )
    assert len(results) == settings.RETRIEVAL_TOP_K
    assert all(r.retrieved_by == "fusion" for r in results)


@pytest.mark.full_corpus
async def test_dated_chunk_in_top_k_adds_up_to_n_companions(pool, real_embedder):
    """A query whose fused top-k DOES contain a dated chunk must add at most
    Settings.DATED_RULE_COMPANIONS more rows, each `retrieved_by == "dated_companion"`, each
    carrying a non-null rule_effective_date equal to one already present among the fusion rows,
    and none duplicating a fusion row's id.
    """
    settings = get_settings()
    [embedding] = await real_embedder.embed([_HAS_DATED_CHUNK_QUERY])

    results = await hybrid_search(
        pool,
        embedding,
        _HAS_DATED_CHUNK_QUERY,
        k=settings.RETRIEVAL_TOP_K,
        rrf_k=settings.RRF_K,
        candidate_pool=settings.HYBRID_CANDIDATE_POOL,
        dated_rule_companions=2,
    )

    fusion = [r for r in results if r.retrieved_by == "fusion"]
    companions = [r for r in results if r.retrieved_by == "dated_companion"]
    fusion_ids = {r.id for r in fusion}
    fusion_dates = {r.rule_effective_date for r in fusion if r.rule_effective_date is not None}

    assert len(fusion) == settings.RETRIEVAL_TOP_K
    assert fusion_dates, (
        f"expected {_HAS_DATED_CHUNK_QUERY!r} to retrieve a dated chunk in its fused top-k -- "
        f"this test no longer exercises the case it is named for: "
        f"{[(r.id, r.rule_effective_date) for r in fusion]}"
    )
    assert (
        len(results) <= settings.RETRIEVAL_TOP_K + 2
    ), f"expected at most k+2 rows, got {len(results)}: {[r.id for r in results]}"
    assert (
        companions
    ), "expected at least one dated companion row for a query with a dated fusion hit"
    for r in companions:
        assert r.rule_effective_date is not None, f"companion {r.id} carries no rule_effective_date"
        assert r.rule_effective_date in fusion_dates, (
            f"companion {r.id}'s rule_effective_date {r.rule_effective_date} is not one already "
            f"present among the fusion rows {fusion_dates}"
        )
        assert r.id not in fusion_ids, f"companion {r.id} duplicates a fusion row id"


# "30" and a "depart..." word in the SAME sentence -- not merely the same chunk -- so this cannot
# be satisfied by an unrelated "30" (a form number, a percentage, a minute count) sharing a long
# chunk with an unrelated "departure" mention elsewhere in it. Written as content regexes, not an
# id list, so this survives a re-ingest that renumbers chunks.
_DEPART_RE = re.compile(r"\bdepart\w*\b", re.IGNORECASE)
_THIRTY_RE = re.compile(r"\b30\b")
_SIXTY_DAY_RE = re.compile(r"60[\s-]days?", re.IGNORECASE)


def _states_new_departure_period(content: str) -> bool:
    return any(
        _THIRTY_RE.search(sentence) and _DEPART_RE.search(sentence)
        for sentence in re.split(r"(?<=[.!?])\s+", content)
    )


@pytest.mark.full_corpus
@pytest.mark.parametrize(
    "question",
    [
        "How many days do I have to depart the US after OPT ends?",
        "What is the grace period after OPT ends?",
        "How long do I have to leave the US after OPT ends?",
    ],
)
async def test_dated_rule_companions_surface_both_the_new_and_current_departure_period(
    pool, real_embedder, question
):
    """The acceptance case docs/adr/0019-dated-rule-companion-retrieval.md exists for: plain RRF
    fusion alone answers each of these questions with only the still-current 60-day rule, because
    the chunk that states the new 30-day replacement loses the fusion race outright. With
    Settings.DATED_RULE_COMPANIONS on, the retrieved set must contain BOTH a dated chunk stating
    the new period AND a chunk still stating the current one, so the generator has what it needs
    to state both rules with their dates (ARCHITECTURE.md, "Answers state both the current rule
    and its dated replacement").
    """
    settings = get_settings()
    [embedding] = await real_embedder.embed([question])

    results = await hybrid_search(
        pool,
        embedding,
        question,
        k=settings.RETRIEVAL_TOP_K,
        rrf_k=settings.RRF_K,
        candidate_pool=settings.HYBRID_CANDIDATE_POOL,
        dated_rule_companions=settings.DATED_RULE_COMPANIONS,
    )

    assert any(
        r.rule_effective_date is not None and _states_new_departure_period(r.content)
        for r in results
    ), (
        f"expected a dated chunk stating the new 30-day departure period for {question!r}, got "
        f"{[(r.id, r.retrieved_by, r.rule_effective_date, r.section_heading) for r in results]}"
    )
    assert any(_SIXTY_DAY_RE.search(r.content) for r in results), (
        f"expected at least one retrieved chunk to still mention the 60-day period for "
        f"{question!r}, got {[(r.id, r.section_heading) for r in results]}"
    )


class _ExplodingLLM(LLM):
    async def generate(self, system: str, user: str) -> str:
        raise AssertionError("LLM.generate must not be called when the no-answer gate fires")


class _ExplodingPool:
    def __getattr__(self, name):
        raise AssertionError(f"pool.{name} must not be touched -- hybrid_search is faked below")


def _fake_chunk(**overrides) -> RetrievedChunk:
    defaults = dict(
        id=1,
        content="chunk text",
        source_url="https://example.gov/a",
        resolved_url=None,
        section_heading="Heading",
        heading_level=2,
        page_last_updated=None,
        rule_effective_date=None,
        fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
        last_verified_at=datetime(2026, 8, 1, tzinfo=UTC),
        distance=0.1,
        rrf_score=0.5,
        semantic_rank=1,
        keyword_rank=None,
        retrieved_by="fusion",
    )
    defaults.update(overrides)
    return RetrievedChunk(**defaults)


async def test_no_answer_gate_ignores_companion_chunks(monkeypatch):
    """Unit test, fakes only, no DB: a companion is admitted because a dated rule is in play, not
    because it is relevant, so it must never be able to flip a genuine NO_ANSWER into an answer.
    Fakes app.pipeline's own hybrid_search reference with one FUSION chunk ABOVE
    NO_ANSWER_MAX_DISTANCE and one DATED_COMPANION chunk BELOW it -- if the no-answer gate
    incorrectly folded the companion into its minimum-distance calculation, this would produce an
    ANSWER instead of NO_ANSWER.
    """
    settings = Settings(LLM_PROVIDER="stub", EMBED_PROVIDER="stub", NO_ANSWER_MAX_DISTANCE=0.5)
    chunks = [
        _fake_chunk(id=1, distance=0.9, retrieved_by="fusion"),
        _fake_chunk(
            id=2,
            distance=0.05,
            retrieved_by="dated_companion",
            rule_effective_date=date(2026, 9, 15),
        ),
    ]

    async def _fake_hybrid_search(*args, **kwargs):
        del args, kwargs
        return chunks

    monkeypatch.setattr("app.pipeline.hybrid_search", _fake_hybrid_search)

    response = await answer_question(
        "What does the fixed period of admission rule say for F-1 students?",
        pool=_ExplodingPool(),
        embedder=StubEmbedder(dim=768),
        llm=_ExplodingLLM(),
        settings=settings,
    )

    assert response.response_type == ResponseType.NO_ANSWER.value, (
        f"expected NO_ANSWER (the only fusion chunk is above NO_ANSWER_MAX_DISTANCE), got "
        f"{response.response_type!r} -- the no-answer gate appears to be using the companion "
        "chunk's distance"
    )


# =================================================================================================
# Phase 8 round 5: retrieval is a SAFETY guardrail, not optional infrastructure, and must never be
# wrapped the way app/usage.py and app/cache.py's optional-infrastructure functions now are (see
# both modules' own docstrings, "OPTIONAL INFRASTRUCTURE, NEVER FATAL"). A database error here must
# keep propagating out of hybrid_search, through app/pipeline.py, to app/main.py's existing 502
# path -- ARCHITECTURE.md's "say when you do not know" and "cite and verify" both depend on
# retrieval actually having run, so a database error here must never be silently swallowed into an
# empty or fabricated result the way it would be reasonable to swallow a failed usage counter.
# =================================================================================================


async def test_hybrid_search_propagates_a_database_error_rather_than_degrading():
    """Proved against a pool that can never successfully connect (a database name guaranteed not
    to exist on this Postgres server -- never the shared corpus database every other test in this
    file uses, so this cannot affect them), with a short pool `timeout` constructed directly here
    (NOT a change to app/db.py::make_pool, which does not expose one) so this test fails fast
    instead of waiting out the default 30-second pool timeout.

    hybrid_search itself contains no try/except at all -- unlike app/usage.py's record_query/
    budget_exceeded/record_generation_call or app/cache.py's corpus_version/lookup/store, all of
    which now catch a database error and degrade. This test's real assertion is behavioral, not
    textual: hybrid_search must actually raise here, proving there is nothing in its call path
    catching this and returning an empty/fabricated result instead.
    """
    from pgvector.psycopg import register_vector_async
    from psycopg_pool import AsyncConnectionPool, PoolTimeout

    settings = get_settings()
    base_url = os.environ.get("DATABASE_URL", settings.DATABASE_URL)
    # Swap only the database name in the URL for one that cannot exist, keeping host/port/user/
    # password exactly as configured -- this is what makes every connection attempt fail with a
    # real, immediate "database does not exist" error rather than a slow network timeout.
    broken_url = base_url.rsplit("/", 1)[0] + "/officehours_guardrail_fatal_test_does_not_exist"

    async def _configure(conn) -> None:
        await register_vector_async(conn)

    broken_pool = AsyncConnectionPool(
        broken_url, open=False, configure=_configure, timeout=2, min_size=1, max_size=2
    )
    await broken_pool.open(wait=False)
    try:
        # PoolTimeout, specifically: every connection attempt to a nonexistent database fails
        # immediately, and the pool keeps retrying (rather than surfacing that immediate failure
        # directly) until its own `timeout` elapses. Whichever concrete exception a real broken
        # table would raise (psycopg.errors.UndefinedTable in the three optional-infrastructure
        # tests above), the point this test proves is the same either way: hybrid_search has no
        # try/except of its own to turn ANY of these into a degraded, non-raising result.
        with pytest.raises(PoolTimeout):
            await hybrid_search(
                broken_pool,
                [0.0] * 768,
                "test question",
                k=5,
                rrf_k=60,
                candidate_pool=20,
                dated_rule_companions=0,
            )
    finally:
        await broken_pool.close()
