"""Semantic cache tests (app/cache.py, Phase 8 round 3).

DB-backed tests use DATABASE_URL from the environment, the same convention every other DB test
file in this directory uses (see test_freshness.py's module docstring) -- point it at a scratch
database, never at the live corpus. Distance-threshold tests below construct their own hand-picked
unit vectors with a known, computed cosine distance, so they never depend on a real embedder or on
the derivation measurements recorded in app/config.py's own comment. The one test that changes the
corpus (test_cache_is_invalidated_when_the_corpus_changes) writes its own `sources`/`documents` row
via test_freshness's shared _insert_test_row/_delete_test_rows helpers, guarded the same way every
other DB-writing test in this directory is guarded against running against the real corpus.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
from pgvector import Vector
from pgvector.psycopg import register_vector_async
from test_freshness import (
    _delete_test_rows,
    _insert_test_row,
    _refuse_if_target_is_the_fully_ingested_real_corpus,
)

from app import cache
from app.config import Settings, get_settings
from app.db import make_pool
from app.pipeline import answer_question
from app.providers.embeddings import Embedder, OllamaEmbedder, StubEmbedder
from app.providers.llm import StubLLM
from app.schemas import AnswerResponse, Citation, ResponseType, RetrievedContext

_DIM = 768


def _unit_vector(index: int) -> list[float]:
    v = [0.0] * _DIM
    v[index] = 1.0
    return v


def _vector_at_cosine_distance_from_e0(delta: float) -> list[float]:
    """A vector `[1, delta, 0, ...]`, normalized -- its cosine distance from `_unit_vector(0)` is
    exactly `1 - 1/sqrt(1 + delta**2)`, computed independently below wherever it matters, so a test
    can pick a delta that lands cleanly on either side of a threshold instead of guessing.
    """
    v = [0.0] * _DIM
    v[0] = 1.0
    v[1] = delta
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return 1.0 - dot / (norm_a * norm_b)


def _fake_answer_response(
    response_type: ResponseType, answer: str = "a cached test answer"
) -> AnswerResponse:
    return AnswerResponse(
        answer=answer,
        citations=[Citation(source_url="https://example.gov/test", chunk_id=1, snippet="snip")],
        contexts=[
            RetrievedContext(
                chunk_id=1,
                source_url="https://example.gov/test",
                section_heading="Test Heading",
                content="test content",
            )
        ],
        response_type=response_type.value,
        refusal_reason=None,
        generated_at=datetime.now(UTC),
    )


@pytest.fixture
def database_url() -> str:
    return os.environ.get("DATABASE_URL", get_settings().DATABASE_URL)


@pytest.fixture
async def pool(database_url):
    p = make_pool(database_url)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture(autouse=True)
async def _clear_query_cache(pool):
    """Every test in this file starts and ends with an empty query_cache table, so tests never
    leak rows into each other regardless of execution order."""

    async def _clear() -> None:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM query_cache")

    await _clear()
    yield
    await _clear()


# =================================================================================================
# corpus_version
# =================================================================================================


async def test_corpus_version_changes_when_a_source_is_reindexed(pool, database_url):
    guard_conn = await psycopg.AsyncConnection.connect(database_url)
    try:
        await _refuse_if_target_is_the_fully_ingested_real_corpus(guard_conn, database_url)
    finally:
        await guard_conn.close()

    source_url = "https://example.gov/cache-corpus-version-test"
    write_conn = await psycopg.AsyncConnection.connect(database_url)
    await register_vector_async(write_conn)
    try:
        await _insert_test_row(write_conn, source_url, fetched_at=datetime(2020, 1, 1, tzinfo=UTC))
        await write_conn.commit()
        version_before = await cache.corpus_version(pool)

        # A later _insert_test_row call on the SAME source_url is exactly what a meaningful
        # re-index looks like from `sources`' point of view: last_changed_at moves forward.
        await _insert_test_row(write_conn, source_url, fetched_at=datetime.now(UTC))
        await write_conn.commit()
        version_after = await cache.corpus_version(pool)

        assert version_after != version_before, (
            "corpus_version() did not change after a source's last_changed_at moved forward -- "
            "the semantic cache would keep serving entries from the OLD corpus state"
        )
    finally:
        await _delete_test_rows(write_conn, source_url)
        await write_conn.close()


# =================================================================================================
# lookup / store: threshold behavior, using hand-picked vectors with a known cosine distance
# =================================================================================================


async def test_lookup_is_a_miss_against_an_empty_cache(pool):
    result = await cache.lookup(
        pool, _unit_vector(0), is_advice=False, threshold=0.15, version="v1"
    )
    assert result is None


async def test_store_then_lookup_at_the_same_embedding_is_a_hit(pool):
    response = _fake_answer_response(ResponseType.ANSWER)
    embedding = _unit_vector(0)

    await cache.store(pool, embedding, response, version="v1")
    result = await cache.lookup(pool, embedding, is_advice=False, threshold=0.15, version="v1")

    assert result is not None
    assert result.answer == response.answer
    assert result.response_type == response.response_type
    assert result.citations == response.citations


async def test_lookup_hits_within_threshold_and_misses_beyond_it(pool):
    """Two hand-picked query vectors against the SAME cached entry: one within
    Settings.SEMANTIC_CACHE_SIMILARITY_THRESHOLD's default (0.15), one beyond it -- proving the
    threshold comparison itself, independent of any real embedder.
    """
    stored_embedding = _unit_vector(0)
    response = _fake_answer_response(ResponseType.ANSWER)
    await cache.store(pool, stored_embedding, response, version="v1")

    near = _vector_at_cosine_distance_from_e0(0.3)  # distance ~= 0.0425
    far = _vector_at_cosine_distance_from_e0(1.0)  # distance ~= 0.2929
    assert _cosine_distance(_unit_vector(0), near) < 0.15
    assert _cosine_distance(_unit_vector(0), far) > 0.15

    hit = await cache.lookup(pool, near, is_advice=False, threshold=0.15, version="v1")
    miss = await cache.lookup(pool, far, is_advice=False, threshold=0.15, version="v1")

    assert hit is not None, "expected a cache hit within the threshold"
    assert miss is None, "expected a cache miss beyond the threshold"


async def test_lookup_ignores_a_matching_embedding_from_a_different_corpus_version(pool):
    """The exact same embedding, stored under version 'v1', must NOT be found by a lookup under
    version 'v2' -- this is the invalidation mechanism's other half (store() also actively deletes
    old-version rows; see test_store_deletes_rows_from_a_superseded_corpus_version below).
    """
    embedding = _unit_vector(0)
    await cache.store(pool, embedding, _fake_answer_response(ResponseType.ANSWER), version="v1")

    result = await cache.lookup(pool, embedding, is_advice=False, threshold=0.15, version="v2")

    assert result is None


# =================================================================================================
# Phase 8 round 4: lookup() partitions on the advice/information classification, not just on the
# corpus version -- a cosine-distance threshold alone cannot separate "about X" from "about X, but
# personal" (see app/cache.py's module docstring for the full reasoning and the real measurements).
# =================================================================================================


@pytest.mark.parametrize(
    "stored_is_advice,query_is_advice",
    [(False, True), (True, False)],
)
async def test_lookup_never_crosses_the_advice_information_boundary(
    pool, stored_is_advice, query_is_advice
):
    """THE WHOLE-CLASS GUARD: a cache row stored under ONE classification must never be returned to
    a query classified the OTHER way, no matter how close the embeddings are -- parametrized over
    BOTH directions (a factual row served to an advice query, and an advice row served to a factual
    query), so this fails on any mismatch, not just the one direction the original bug report
    happened to describe.

    `near` is a hand-picked vector at cosine distance ~=0.0425 from the stored embedding --
    comfortably inside SEMANTIC_CACHE_SIMILARITY_THRESHOLD's default (0.15), i.e. exactly the
    "these two would collide under the old, classification-blind lookup()" case.
    """
    stored_response_type = ResponseType.REFUSAL_ADVICE if stored_is_advice else ResponseType.ANSWER
    await cache.store(
        pool, _unit_vector(0), _fake_answer_response(stored_response_type), version="v1"
    )

    near = _vector_at_cosine_distance_from_e0(0.3)  # distance ~= 0.0425
    assert _cosine_distance(_unit_vector(0), near) < 0.15

    result = await cache.lookup(pool, near, is_advice=query_is_advice, threshold=0.15, version="v1")

    assert result is None, (
        f"lookup() returned a response cached under is_advice={stored_is_advice} to a query "
        f"classified is_advice={query_is_advice} -- the semantic cache crossed the advice/"
        "information boundary despite the embeddings being well within threshold"
    )


@pytest.mark.parametrize("is_advice", [False, True])
async def test_lookup_still_hits_when_the_classification_matches(pool, is_advice):
    """The other half of the same guard: partitioning on classification must not make the cache
    stop working when the classification genuinely DOES match -- a hit is still a hit within
    threshold, for BOTH classifications, not just the historically-tested ANSWER case.
    """
    response_type = ResponseType.REFUSAL_ADVICE if is_advice else ResponseType.ANSWER
    await cache.store(pool, _unit_vector(0), _fake_answer_response(response_type), version="v1")

    near = _vector_at_cosine_distance_from_e0(0.3)  # distance ~= 0.0425
    result = await cache.lookup(pool, near, is_advice=is_advice, threshold=0.15, version="v1")

    assert result is not None, (
        f"expected a cache hit for is_advice={is_advice} when the stored row was cached under the "
        "SAME classification and the embeddings are well within threshold"
    )
    assert result.response_type == response_type.value


# =================================================================================================
# store: only ANSWER/REFUSAL_ADVICE are ever cached
# =================================================================================================


async def _query_cache_row_count(pool) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM query_cache")
            (count,) = await cur.fetchone()
    return count


@pytest.mark.parametrize(
    "response_type",
    [ResponseType.CLARIFY, ResponseType.NO_ANSWER, ResponseType.BLOCKED_UNVERIFIED],
)
async def test_store_never_caches_clarify_no_answer_or_blocked_unverified(pool, response_type):
    response = _fake_answer_response(response_type)

    await cache.store(pool, _unit_vector(0), response, version="v1")

    assert await _query_cache_row_count(pool) == 0


@pytest.mark.parametrize("response_type", [ResponseType.ANSWER, ResponseType.REFUSAL_ADVICE])
async def test_store_caches_answer_and_refusal_advice(pool, response_type):
    response = _fake_answer_response(response_type)

    await cache.store(pool, _unit_vector(0), response, version="v1")

    assert await _query_cache_row_count(pool) == 1


# =================================================================================================
# store: a corpus-version change wipes every superseded-version row outright
# =================================================================================================


async def test_store_deletes_rows_from_a_superseded_corpus_version(pool):
    await cache.store(
        pool, _unit_vector(0), _fake_answer_response(ResponseType.ANSWER), version="v1"
    )
    assert await _query_cache_row_count(pool) == 1

    await cache.store(
        pool, _unit_vector(1), _fake_answer_response(ResponseType.ANSWER), version="v2"
    )

    # The v1 row is gone entirely, not merely unreachable -- store() deletes every row whose
    # corpus_version != the version it is about to write under.
    assert await _query_cache_row_count(pool) == 1
    assert (
        await cache.lookup(pool, _unit_vector(0), is_advice=False, threshold=0.15, version="v1")
        is None
    )


# =================================================================================================
# THE key proof: a real corpus change (via answer_question end to end) invalidates a cached answer
# =================================================================================================


async def test_cache_is_invalidated_when_the_corpus_changes(pool, database_url):
    """Runs the real pipeline three times against the SAME question, with the semantic cache
    turned on:

    1. First call: cache is empty -> a real generation happens and gets cached.
    2. Second call, same question, corpus unchanged: served FROM THE CACHE -- proved by
       `generated_at` being IDENTICAL to the first call's (impossible for two independent
       generations, which each stamp their own `datetime.now(UTC)`).
    3. The corpus is then changed for real (a new source is inserted, moving
       `sources.last_changed_at` and `documents`' row count -- exactly what app/recrawl.py's
       reindex_source does on a meaningful change). A third call with the SAME question must NOT
       be served from the stale cache entry: `generated_at` must differ from the first two calls,
       proving a fresh generation happened instead.
    """
    guard_conn = await psycopg.AsyncConnection.connect(database_url)
    try:
        await _refuse_if_target_is_the_fully_ingested_real_corpus(guard_conn, database_url)
    finally:
        await guard_conn.close()

    write_conn = await psycopg.AsyncConnection.connect(database_url)
    await register_vector_async(write_conn)

    question = "How long does the cache invalidation test question stay valid for review?"
    settings = Settings(
        LLM_PROVIDER="stub",
        EMBED_PROVIDER="stub",
        NO_ANSWER_MAX_DISTANCE=2.0,
        SEMANTIC_CACHE_ENABLED=True,
        SEMANTIC_CACHE_SIMILARITY_THRESHOLD=0.15,
    )
    embedder = StubEmbedder(dim=768)
    llm = StubLLM()

    new_source_url = "https://example.gov/cache-invalidation-test"
    try:
        first = await answer_question(
            question, pool=pool, embedder=embedder, llm=llm, settings=settings
        )
        assert first.response_type == ResponseType.ANSWER.value

        second = await answer_question(
            question, pool=pool, embedder=embedder, llm=llm, settings=settings
        )
        assert second.generated_at == first.generated_at, (
            "expected the second call to be served verbatim from the cache (identical "
            "generated_at); got a different timestamp, meaning it was regenerated instead of "
            "served from cache"
        )

        # Change the corpus for real.
        await _insert_test_row(write_conn, new_source_url, fetched_at=datetime.now(UTC))
        await write_conn.commit()

        third = await answer_question(
            question, pool=pool, embedder=embedder, llm=llm, settings=settings
        )
        assert third.generated_at != first.generated_at, (
            "expected the third call, made AFTER the corpus changed, to be regenerated (a "
            "different generated_at) rather than served from the now-stale cache entry"
        )
    finally:
        await _delete_test_rows(write_conn, new_source_url)
        await write_conn.close()


# =================================================================================================
# THE real-world reproduction: the exact reported pair, real nomic-embed-text embeddings, driven
# through the real pipeline (app/pipeline.py::answer_question) end to end.
# =================================================================================================


def _real_ollama_reachable(base_url: str) -> bool:
    """Best-effort, fast check for a real, reachable Ollama with nomic-embed-text available. False
    (never raises) for anything short of a clean 200 -- this only decides whether the test below
    runs at all; it never weakens what the test asserts once it does run.
    """
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=2.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


requires_real_ollama_embedder = pytest.mark.skipif(
    not _real_ollama_reachable(get_settings().OLLAMA_BASE_URL),
    reason=(
        "requires a reachable Ollama serving nomic-embed-text (real embeddings) -- not available "
        "in CI's ci-invariant-gate job, which has no GPU and no Ollama; run this locally against a "
        "dev machine with Ollama running (see ARCHITECTURE.md, 'Dev defaults to local Ollama')"
    ),
)


class _PrecomputedEmbedder(Embedder):
    """A fake embedder that returns a FIXED, precomputed vector for each exact question string it
    is given, rather than computing anything itself. Lets a test drive app/pipeline.py::
    answer_question with REAL nomic-embed-text embeddings (computed once, up front, by a real
    OllamaEmbedder -- see the test below) while keeping the rest of the call hermetic and
    order-independent.
    """

    def __init__(self, vectors_by_question: dict[str, list[float]]):
        self._vectors_by_question = vectors_by_question

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors_by_question[text] for text in texts]


@requires_real_ollama_embedder
async def test_cache_never_serves_an_advice_answer_cached_for_a_purely_factual_question(
    pool, database_url
):
    """THE reported failure, reproduced end to end through the real pipeline with REAL embeddings
    of the exact pair the round-4 review measured:

        "Does my employer need E-Verify for the STEM extension?"              (purely informational)
        "Should I switch to an E-Verify employer to get the STEM extension?"  (advice-seeking)

    Real cosine distance between these two, computed here with the same nomic-embed-text embedder
    the query path itself uses: ~=0.0885 -- deep inside SEMANTIC_CACHE_SIMILARITY_THRESHOLD's
    default (0.15). Before Phase 8 round 4's fix, asking the first question (cached as
    response_type=answer) and then the second would have been served that SAME cached answer --
    no refusal, no DSO/attorney redirect -- purely because their embeddings are close, with the
    classifier's own, correctly-computed is_advice=True verdict for the second question thrown
    away. After the fix, the second call must fall through to a REAL generation instead.

    Classification for both questions is decided by Layer 1 (app/guardrails/classifier.py's
    deterministic rule_based_advice_signal), not by a model call, so this stays deterministic under
    LLM_PROVIDER=stub: "should i" matches the second question and nothing in
    app.guardrails.classifier.ADVICE_PATTERNS matches the first.

    Retrieval is decoupled from whatever corpus this DB happens to hold: a single scratch chunk
    (unique source_url, deleted in `finally`) carries the shared keywords both questions use
    ("E-Verify", "STEM", "extension", "employer"), so hybrid_search's keyword arm finds it
    regardless of the real, but otherwise irrelevant here, semantic distance to that chunk's own
    embedding -- and NO_ANSWER_MAX_DISTANCE=2.0 (the maximum possible cosine distance) keeps the
    no-answer gate from ever firing on this scratch corpus either way.
    """
    guard_conn = await psycopg.AsyncConnection.connect(database_url)
    try:
        await _refuse_if_target_is_the_fully_ingested_real_corpus(guard_conn, database_url)
    finally:
        await guard_conn.close()

    factual_question = "Does my employer need E-Verify for the STEM extension?"
    advice_question = "Should I switch to an E-Verify employer to get the STEM extension?"

    chunk_content = (
        "Your employer must be enrolled in E-Verify for you to be eligible for the 24-month "
        "STEM OPT extension."
    )

    settings = get_settings()
    real_embedder = OllamaEmbedder(base_url=settings.OLLAMA_BASE_URL, model=settings.EMBED_MODEL)
    factual_embedding, advice_embedding, chunk_embedding = await real_embedder.embed(
        [factual_question, advice_question, chunk_content]
    )
    real_distance = _cosine_distance(factual_embedding, advice_embedding)
    assert real_distance < 0.15, (
        f"expected this pair's real measured cosine distance to sit inside the 0.15 threshold "
        f"(the whole point of this reproduction); got {real_distance}"
    )

    scripted_embedder = _PrecomputedEmbedder(
        {factual_question: factual_embedding, advice_question: advice_embedding}
    )

    write_conn = await psycopg.AsyncConnection.connect(database_url)
    await register_vector_async(write_conn)
    source_url = "https://example.gov/cache-advice-boundary-test"
    try:
        # Inserted directly (not via test_freshness's _insert_test_row, which hardcodes an all-zero
        # embedding) so this chunk carries a REAL, non-degenerate embedding -- pgvector's cosine
        # distance is undefined for a zero-magnitude vector, and this test has no reason to depend
        # on how that edge case happens to behave.
        now = datetime.now(UTC)
        async with write_conn.transaction():
            async with write_conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO sources
                        (source_url, resolved_url, page_last_updated, fetched_at, last_verified_at,
                         last_changed_at, last_success_at, change_count, consecutive_failures,
                         last_error, last_http_status, status)
                    VALUES (%s, NULL, NULL, %s, %s, %s, %s, 0, 0, NULL, NULL, 'ok')
                    ON CONFLICT (source_url) DO UPDATE SET
                        fetched_at = EXCLUDED.fetched_at,
                        last_verified_at = EXCLUDED.last_verified_at,
                        last_changed_at = EXCLUDED.last_changed_at,
                        last_success_at = EXCLUDED.last_success_at
                    """,
                    (source_url, now, now, now, now),
                )
                await cur.execute("DELETE FROM documents WHERE source_url = %s", (source_url,))
                await cur.execute(
                    """
                    INSERT INTO documents
                        (content, source_url, section_heading, heading_level, rule_effective_date,
                         embedding)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chunk_content,
                        source_url,
                        "STEM OPT Extension Employer Requirements",
                        2,
                        None,
                        Vector(chunk_embedding),
                    ),
                )

        pipeline_settings = Settings(
            LLM_PROVIDER="stub",
            EMBED_PROVIDER="stub",
            NO_ANSWER_MAX_DISTANCE=2.0,
            SEMANTIC_CACHE_ENABLED=True,
            SEMANTIC_CACHE_SIMILARITY_THRESHOLD=0.15,
        )
        llm = StubLLM()

        factual_response = await answer_question(
            factual_question,
            pool=pool,
            embedder=scripted_embedder,
            llm=llm,
            settings=pipeline_settings,
        )
        assert factual_response.response_type == ResponseType.ANSWER.value, (
            "expected the factual question to generate and cache a real ANSWER; got "
            f"{factual_response.response_type!r}"
        )

        advice_response = await answer_question(
            advice_question,
            pool=pool,
            embedder=scripted_embedder,
            llm=llm,
            settings=pipeline_settings,
        )
        assert advice_response.response_type == ResponseType.REFUSAL_ADVICE.value, (
            "the advice-seeking question was served the OTHER question's cached ANSWER "
            f"(response_type={advice_response.response_type!r}) instead of being refused -- the "
            "semantic cache crossed the advice/information boundary"
        )
        assert advice_response.refusal_reason == "query_asks_for_personal_advice"
        lowered = advice_response.answer.lower()
        assert "dso" in lowered or "designated school official" in lowered
        assert "licensed immigration attorney" in lowered
    finally:
        await _delete_test_rows(write_conn, source_url)
        await write_conn.close()


# =================================================================================================
# Phase 8 round 5: the semantic cache is OPTIONAL infrastructure -- corpus_version, lookup, and
# store must all degrade (log a warning, return a safe fallback) rather than raise when
# `query_cache` (or anything corpus_version reads) is missing or unreadable. This is the same bug
# class app/usage.py's record_query hit in production (a live database that predated a table this
# project later added, turning an already-successful answer into a 502) -- see both modules' own
# docstrings, "OPTIONAL INFRASTRUCTURE, NEVER FATAL". `query_cache` is dropped for the duration of
# one test only and recreated exactly as infra/sql/init.sql defines it (including its indexes), so
# no other test in this file ever sees it missing -- and the autouse `_clear_query_cache` fixture
# above still runs around it (DELETE FROM query_cache against a table that, by the time that
# fixture's own teardown runs, has already been recreated).
# =================================================================================================


@pytest.fixture
async def _query_cache_temporarily_dropped(pool):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DROP TABLE query_cache CASCADE")
    try:
        yield
    finally:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS query_cache (
                        id                 BIGSERIAL PRIMARY KEY,
                        embedding          VECTOR(768) NOT NULL,
                        corpus_version     TEXT NOT NULL,
                        response_type      TEXT NOT NULL
                            CHECK (response_type IN ('answer', 'refusal_advice')),
                        response_json      JSONB NOT NULL,
                        created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                await cur.execute(
                    "CREATE INDEX IF NOT EXISTS query_cache_corpus_version_idx "
                    "ON query_cache (corpus_version)"
                )
                await cur.execute(
                    "CREATE INDEX IF NOT EXISTS query_cache_embedding_hnsw "
                    "ON query_cache USING hnsw (embedding vector_cosine_ops)"
                )


async def test_lookup_degrades_to_a_miss_instead_of_raising_when_query_cache_is_missing(
    pool, caplog, _query_cache_temporarily_dropped
):
    with caplog.at_level(logging.WARNING, logger="app.cache"):
        result = await cache.lookup(
            pool, _unit_vector(0), is_advice=False, threshold=0.15, version="v1"
        )  # must not raise

    assert result is None, "a lookup that cannot even reach query_cache must read as a cache miss"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    messages = [r.getMessage() for r in warnings]
    assert any(
        "query_cache" in m for m in messages
    ), f"expected a WARNING naming query_cache; got: {messages}"
    assert any(r.exc_info is not None for r in warnings), "the real exception must be logged"


async def test_store_degrades_instead_of_raising_when_query_cache_is_missing(
    pool, caplog, _query_cache_temporarily_dropped
):
    response = _fake_answer_response(ResponseType.ANSWER)

    with caplog.at_level(logging.WARNING, logger="app.cache"):
        await cache.store(pool, _unit_vector(0), response, version="v1")  # must not raise

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    messages = [r.getMessage() for r in warnings]
    assert any(
        "query_cache" in m for m in messages
    ), f"expected a WARNING naming query_cache; got: {messages}"
    assert any(r.exc_info is not None for r in warnings), "the real exception must be logged"


async def test_corpus_version_returns_none_and_logs_a_warning_on_a_database_error(pool, caplog):
    """corpus_version reads `sources`/`documents`, not `query_cache` -- proved separately here by
    pointing it at a pool that can never connect at all, rather than by dropping a real table this
    file's other tests (and app/pipeline.py's retrieval path) still need. Returning None (rather
    than raising) is what lets app/pipeline.py cleanly skip both the lookup AND the later store call
    for this request (see app/pipeline.py's own Phase 8 round 5 comment on this call site) with no
    extra branching needed there.
    """
    from psycopg_pool import AsyncConnectionPool

    async def _configure(conn) -> None:
        from pgvector.psycopg import register_vector_async

        await register_vector_async(conn)

    broken_pool = AsyncConnectionPool(
        "postgresql://nope:nope@127.0.0.1:1/nope",
        open=False,
        configure=_configure,
        timeout=1,
        min_size=1,
        max_size=1,
    )
    await broken_pool.open(wait=False)
    try:
        with caplog.at_level(logging.WARNING, logger="app.cache"):
            version = await cache.corpus_version(broken_pool)  # must not raise
    finally:
        await broken_pool.close()

    assert version is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "corpus_version" in r.getMessage() for r in warnings
    ), f"expected a WARNING about corpus_version; got: {[r.getMessage() for r in warnings]}"
