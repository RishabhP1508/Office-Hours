"""Usage counter and daily generation budget cap tests (app/usage.py, Phase 8 round 3).

DB-backed tests use DATABASE_URL from the environment, the same convention every other DB test
file in this directory uses. usage_totals/usage_sessions/daily_generation_counts are all shared,
process-wide tables (unlike query_cache, nothing here is scoped to a single test's own rows), so
every assertion below is DELTA-based -- "did this call increase the count by exactly N" -- rather
than asserting an absolute value, so these tests are correct regardless of what else already ran
against the same database, in this test session or a previous one on the same UTC day.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest

from app import usage
from app.config import Settings, get_settings
from app.db import make_pool
from app.pipeline import answer_question
from app.providers.embeddings import StubEmbedder
from app.providers.llm import StubLLM
from app.schemas import ResponseType

# =================================================================================================
# hash_session_identifier: deterministic, salted, never reveals the raw input
# =================================================================================================


def test_hash_session_identifier_is_deterministic():
    a = usage.hash_session_identifier("203.0.113.5", salt="a-real-secret-salt")
    b = usage.hash_session_identifier("203.0.113.5", salt="a-real-secret-salt")
    assert a == b


def test_hash_session_identifier_depends_on_the_salt():
    a = usage.hash_session_identifier("203.0.113.5", salt="salt-one")
    b = usage.hash_session_identifier("203.0.113.5", salt="salt-two")
    assert a != b


def test_hash_session_identifier_distinguishes_different_raw_identifiers():
    a = usage.hash_session_identifier("203.0.113.5", salt="a-real-secret-salt")
    b = usage.hash_session_identifier("203.0.113.6", salt="a-real-secret-salt")
    assert a != b


def test_hash_session_identifier_never_returns_the_raw_input():
    raw = "203.0.113.5"
    hashed = usage.hash_session_identifier(raw, salt="a-real-secret-salt")
    assert hashed != raw
    assert raw not in hashed
    # A hex-encoded SHA-256 digest is always exactly 64 hex characters.
    assert len(hashed) == 64
    assert all(c in "0123456789abcdef" for c in hashed)


# =================================================================================================
# Persistent usage counters: total queries, distinct sessions
# =================================================================================================


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


async def test_record_query_increments_total_queries(pool):
    before = await usage.get_usage_counts(pool)
    await usage.record_query(pool, None)
    after = await usage.get_usage_counts(pool)
    assert after.total_queries == before.total_queries + 1


async def test_record_query_with_no_session_hash_does_not_grow_distinct_sessions(pool):
    before = await usage.get_usage_counts(pool)
    await usage.record_query(pool, None)
    after = await usage.get_usage_counts(pool)
    assert after.distinct_sessions == before.distinct_sessions
    assert after.total_queries == before.total_queries + 1


async def test_record_query_dedupes_the_same_session_hash(pool):
    # usage_sessions accumulates every distinct hash FOREVER (by design -- see app/usage.py), so a
    # fixed raw identifier would only ever be "new" once across this database's whole lifetime. A
    # fresh uuid4 per test run is what makes "distinct_sessions grew by exactly N" a correct
    # assertion on every run, not just the first.
    session_hash = usage.hash_session_identifier(
        f"198.51.100.42-test-usage-dedupe-{uuid.uuid4()}", salt="test-salt"
    )
    before = await usage.get_usage_counts(pool)

    await usage.record_query(pool, session_hash)
    await usage.record_query(pool, session_hash)
    await usage.record_query(pool, session_hash)

    after = await usage.get_usage_counts(pool)
    assert after.total_queries == before.total_queries + 3
    # The SAME session hash recorded three times contributes exactly one distinct session, never
    # three -- usage_sessions is keyed on session_hash with ON CONFLICT DO NOTHING.
    assert after.distinct_sessions == before.distinct_sessions + 1


async def test_record_query_counts_two_different_sessions_separately(pool):
    hash_a = usage.hash_session_identifier(f"198.51.100.10-test-usage-a-{uuid.uuid4()}", salt="s")
    hash_b = usage.hash_session_identifier(f"198.51.100.11-test-usage-b-{uuid.uuid4()}", salt="s")
    before = await usage.get_usage_counts(pool)

    await usage.record_query(pool, hash_a)
    await usage.record_query(pool, hash_b)

    after = await usage.get_usage_counts(pool)
    assert after.total_queries == before.total_queries + 2
    assert after.distinct_sessions == before.distinct_sessions + 2


async def test_usage_counts_persist_across_a_brand_new_pool(database_url):
    """The restart-persistence proof at the unit level: `usage.record_query` through one pool
    object, then read back through a SECOND, entirely independent AsyncConnectionPool -- there is
    no shared Python state between the two beyond the same Postgres database, exactly the same
    relationship a restarted orchestrator process has with the database it reconnects to.
    """
    session_hash = usage.hash_session_identifier(
        f"198.51.100.99-test-usage-restart-{uuid.uuid4()}", salt="test-salt"
    )

    pool_a = make_pool(database_url)
    await pool_a.open()
    try:
        before = await usage.get_usage_counts(pool_a)
        await usage.record_query(pool_a, session_hash)
    finally:
        await pool_a.close()

    pool_b = make_pool(database_url)
    await pool_b.open()
    try:
        after = await usage.get_usage_counts(pool_b)
    finally:
        await pool_b.close()

    assert after.total_queries == before.total_queries + 1
    assert after.distinct_sessions == before.distinct_sessions + 1


# =================================================================================================
# Daily generation budget cap
# =================================================================================================


async def test_get_generation_count_today_and_record_generation_call_increment(pool):
    before = await usage.get_generation_count_today(pool)
    await usage.record_generation_call(pool)
    await usage.record_generation_call(pool)
    after = await usage.get_generation_count_today(pool)
    assert after == before + 2


async def test_budget_exceeded_false_before_the_cap_true_once_reached(pool):
    current = await usage.get_generation_count_today(pool)
    cap = current + 2

    assert await usage.budget_exceeded(pool, cap) is False

    await usage.record_generation_call(pool)
    assert await usage.budget_exceeded(pool, cap) is False

    await usage.record_generation_call(pool)
    assert await usage.budget_exceeded(pool, cap) is True


async def test_budget_exceeded_is_false_for_a_non_positive_cap_without_touching_the_database():
    """cap <= 0 means "no cap configured" and must short-circuit before any database round trip --
    proved here with a pool pointed at an unreachable host that is NEVER opened: if
    budget_exceeded tried to use it at all, this would hang or raise instead of returning promptly.
    """
    unreachable_pool = make_pool("postgresql://nope:nope@198.51.100.254:5432/nope")
    assert await usage.budget_exceeded(unreachable_pool, 0) is False
    assert await usage.budget_exceeded(unreachable_pool, -1) is False


# =================================================================================================
# The daily generation budget cap wired through the real pipeline (app/pipeline.py)
# =================================================================================================


@pytest.fixture
def embedder():
    return StubEmbedder(dim=768)


@pytest.fixture
def llm():
    return StubLLM()


async def test_answer_question_generates_normally_under_the_cap(pool, embedder, llm):
    current = await usage.get_generation_count_today(pool)
    settings = Settings(
        LLM_PROVIDER="stub",
        EMBED_PROVIDER="stub",
        NO_ANSWER_MAX_DISTANCE=2.0,
        DAILY_GENERATION_CAP=current + 10,
    )

    response = await answer_question(
        "How long is the STEM OPT extension?",
        pool=pool,
        embedder=embedder,
        llm=llm,
        settings=settings,
    )

    assert response.response_type == ResponseType.ANSWER.value
    after = await usage.get_generation_count_today(pool)
    assert after == current + 1, "a real generation call must increment today's UTC count by one"


async def test_answer_question_degrades_without_erroring_once_the_cap_is_reached(
    pool, embedder, llm
):
    current = await usage.get_generation_count_today(pool)
    if current == 0:
        # budget_exceeded checks count >= cap, and cap <= 0 means "no cap" -- so a cap that is
        # ALREADY reached requires today's count to be at least 1. Bump it once first if today's
        # UTC day has recorded nothing yet, so the cap chosen below (== current) is guaranteed to
        # already be tripped rather than accidentally disabled.
        await usage.record_generation_call(pool)
        current = await usage.get_generation_count_today(pool)
    settings = Settings(
        LLM_PROVIDER="stub",
        EMBED_PROVIDER="stub",
        NO_ANSWER_MAX_DISTANCE=2.0,
        # count >= cap is already true (cap == current), so this call must be refused before
        # generate() is ever attempted.
        DAILY_GENERATION_CAP=current,
    )

    response = await answer_question(
        "How long is the STEM OPT extension?",
        pool=pool,
        embedder=embedder,
        llm=llm,
        settings=settings,
    )

    # Never a 500/exception (this call must return normally), never an ungrounded fabricated
    # answer: response_type stays NO_ANSWER (see app/pipeline.py's _BUDGET_EXCEEDED_MESSAGE comment
    # for why this reuses that type), refusal_reason distinguishes it from a real "sources don't
    # cover it" no-answer, and the generator was never actually called -- the count must NOT have
    # moved.
    assert response.response_type == ResponseType.NO_ANSWER.value
    assert response.refusal_reason == "daily_generation_cap_reached"
    assert (
        response.citations
    ), "the degraded response must still point at the real retrieved sources"
    assert "daily limit" in response.answer.lower()

    after = await usage.get_generation_count_today(pool)
    assert after == current, "the generator must never actually be called once the cap is reached"


# =================================================================================================
# Phase 8 round 5: usage_totals/usage_sessions and daily_generation_counts are OPTIONAL
# infrastructure -- a missing table (or any other database error reading/writing one) must degrade
# with a logged warning/error, never raise. Reproduces the real production incident directly: a live
# database that predates one of these tables threw psycopg.errors.UndefinedTable from
# record_query's UPDATE, which app/main.py's blanket except turned into a 502 for an answer that had
# already been generated and verified. Each fixture below drops the table for the duration of one
# test only, then recreates it exactly as infra/sql/init.sql defines it, so no other test in this
# file (or this session) ever sees it missing.
# =================================================================================================


@pytest.fixture
async def _usage_totals_temporarily_dropped(pool):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DROP TABLE usage_totals CASCADE")
    try:
        yield
    finally:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_totals (
                        id             INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                        total_queries  BIGINT NOT NULL DEFAULT 0
                    )
                    """
                )
                await cur.execute(
                    "INSERT INTO usage_totals (id, total_queries) VALUES (1, 0) "
                    "ON CONFLICT (id) DO NOTHING"
                )


@pytest.fixture
async def _daily_generation_counts_temporarily_dropped(pool):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DROP TABLE daily_generation_counts CASCADE")
    try:
        yield
    finally:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_generation_counts (
                        day                DATE PRIMARY KEY,
                        generation_count   INT NOT NULL DEFAULT 0
                    )
                    """
                )


async def test_record_query_degrades_instead_of_raising_when_usage_totals_is_missing(
    pool, caplog, _usage_totals_temporarily_dropped
):
    """The exact production bug this fix closes: record_query's UPDATE against usage_totals must
    never propagate. It must log at WARNING with the real exception and return normally, so
    app/main.py's POST /query handler still returns the answer it already generated.
    """
    with caplog.at_level(logging.WARNING, logger="app.usage"):
        await usage.record_query(pool, "some-session-hash")  # must not raise

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "usage_totals" in r.getMessage() for r in warnings
    ), f"expected a WARNING naming usage_totals; got: {[r.getMessage() for r in warnings]}"
    assert any(
        r.exc_info is not None for r in warnings
    ), "the real exception must be logged (exc_info=True), not swallowed silently"


async def test_budget_exceeded_fails_open_and_logs_loudly_when_the_table_is_missing(
    pool, caplog, _daily_generation_counts_temporarily_dropped
):
    """FAIL OPEN, DELIBERATELY: see budget_exceeded's own docstring for why a cap that cannot even
    be read must return False (not exceeded) rather than propagate and turn this analytics table's
    outage into a hard failure of the whole answer path. Logged at ERROR, louder than a plain
    WARNING, because failing open silently defeats the cap an operator turned on -- they need to
    know it stopped being enforced.
    """
    with caplog.at_level(logging.WARNING, logger="app.usage"):
        exceeded = await usage.budget_exceeded(pool, cap=1)

    assert exceeded is False, "a cap that cannot be read must fail OPEN, never fail closed"
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(
        "daily_generation_counts" in r.getMessage() for r in errors
    ), f"expected an ERROR naming daily_generation_counts; got: {[r.getMessage() for r in errors]}"
    assert any(r.exc_info is not None for r in errors), "the real exception must be logged"


async def test_record_generation_call_degrades_instead_of_raising_when_the_table_is_missing(
    pool, caplog, _daily_generation_counts_temporarily_dropped
):
    """Symmetric with the read-side test above, for the write side: a generation call that already
    happened (this is only ever called AFTER llm.generate() succeeds -- see app/pipeline.py) must
    not fail to be answered just because its cost counter could not be persisted.
    """
    with caplog.at_level(logging.WARNING, logger="app.usage"):
        await usage.record_generation_call(pool)  # must not raise

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    messages = [r.getMessage() for r in warnings]
    assert any(
        "daily_generation_counts" in m for m in messages
    ), f"expected a WARNING naming daily_generation_counts; got: {messages}"
    assert any(r.exc_info is not None for r in warnings), "the real exception must be logged"
