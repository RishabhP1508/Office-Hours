"""POST /query happy path against a live stack, plus (Phase 6) POST /query/stream, GET
/sources/status, and the additive on_event plumbing -- all in-process against app.main.app, with
deterministic stub providers (LLM_PROVIDER=stub, EMBED_PROVIDER=stub), the same convention every
other test file in this directory uses (see tests/test_guardrails.py's module docstring). These
never need a reachable Ollama or the live_stack marker below -- only the original happy-path test
does.
"""

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pgvector.psycopg import register_vector_async
from test_freshness import (
    _delete_test_rows,
    _insert_test_row,
    _refuse_if_target_is_the_fully_ingested_real_corpus,
)

from app.config import Settings, get_settings
from app.db import make_pool
from app.guardrails.freshness import sources_freshness_state
from app.main import app, sources_status
from app.pipeline import answer_question
from app.providers.embeddings import StubEmbedder
from app.providers.llm import StubLLM
from app.schemas import AnswerResponse, ResponseType

# Defaults to the compose service hostname because this test is meant to be run with
# `docker compose run --rm orchestrator pytest tests/test_query.py`, which starts a second
# container on the same compose network as the already-running `orchestrator` service. Override to
# http://localhost:8000 when running against the published port from the host instead.
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:8000")


@pytest.mark.live_stack
def test_query_happy_path_returns_grounded_citation():
    response = httpx.post(
        f"{ORCHESTRATOR_URL}/query",
        json={"question": "How long is the STEM OPT extension?"},
        timeout=300.0,
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["answer"].strip(), "Expected a non-empty answer"
    assert body["citations"], "Expected at least one citation"
    assert body["disclaimer"], "Expected a disclaimer on every answer"

    cited_urls = {c["source_url"] for c in body["citations"]}
    settings = get_settings()
    with psycopg.connect(settings.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # Phase 7: resolved_url moved off `documents` onto `sources` (one row per source).
            cur.execute("SELECT source_url, resolved_url FROM sources")
            rows = cur.fetchall()
    known_urls = set()
    for source_url, resolved_url in rows:
        known_urls.add(source_url)
        if resolved_url:
            known_urls.add(resolved_url)

    assert cited_urls & known_urls, (
        f"None of the cited URLs {cited_urls} match a source_url/resolved_url in the documents "
        "table; citations must point at chunks that were actually retrieved."
    )


# =================================================================================================
# Phase 6: POST /query/stream, GET /sources/status, and the on_event default guard.
#
# `stub_client` below runs app.main.app's real lifespan (a real Postgres pool opens against
# DATABASE_URL/Settings' default, exactly like tests/test_guardrails.py's own `pool` fixture --
# whatever corpus is actually loaded there), then swaps app.state.settings/embedder/llm for
# deterministic stub-provider values so these tests never depend on a reachable Ollama. Retrieval
# results are meaningless under the stub embedder's content-blind hash (see
# app/providers/embeddings.py::StubEmbedder's own docstring), but nothing below asserts on answer
# quality -- only on the stage-event plumbing and response shape, which do not depend on it.
# =================================================================================================


@pytest.fixture
def stub_client():
    with TestClient(app) as client:
        app.state.settings = Settings(
            LLM_PROVIDER="stub", EMBED_PROVIDER="stub", NO_ANSWER_MAX_DISTANCE=2.0
        )
        app.state.embedder = StubEmbedder(dim=768)
        app.state.llm = StubLLM()
        yield client


def _read_sse_events(response) -> list[dict]:
    """Parse `data: <json>\\n\\n`-framed SSE lines back into the dicts app/main.py's
    POST /query/stream put on the wire, in the order they arrived."""
    events = []
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        events.append(json.loads(line[len("data: ") :]))
    return events


def test_query_stream_yields_stage_events_in_pipeline_order_and_terminates_with_a_result(
    stub_client,
):
    with stub_client.stream(
        "POST", "/query/stream", json={"question": "How long is the STEM OPT extension?"}
    ) as response:
        assert response.status_code == 200
        events = _read_sse_events(response)

    assert events[0] == {"event": "ping"}

    stage_events = [e for e in events if e["event"] == "stage"]
    assert [(e["stage"], e["status"]) for e in stage_events] == [
        ("classify", "start"),
        ("classify", "done"),
        ("retrieve", "start"),
        ("retrieve", "done"),
        ("generate", "start"),
        ("generate", "done"),
        ("verify", "start"),
        ("verify", "done"),
    ], f"stage events did not arrive in pipeline order: {stage_events}"

    result_events = [e for e in events if e["event"] == "result"]
    assert len(result_events) == 1, f"expected exactly one result event, got: {events}"
    response_body = result_events[0]["response"]
    # Must parse as a real AnswerResponse -- proves /query/stream's terminal message carries the
    # exact same response shape POST /query returns, not a hand-shaped subset.
    parsed = AnswerResponse.model_validate(response_body)
    assert parsed.response_type in {rt.value for rt in ResponseType}

    retrieve_done = next(
        e for e in stage_events if e["stage"] == "retrieve" and e["status"] == "done"
    )
    assert retrieve_done["source_count"] == len(response_body["citations"])


def test_query_stream_vague_query_emits_only_classify_events_never_retrieve_generate_or_verify(
    stub_client,
):
    with stub_client.stream("POST", "/query/stream", json={"question": "help"}) as response:
        assert response.status_code == 200
        events = _read_sse_events(response)

    stage_events = [e for e in events if e["event"] == "stage"]
    assert [(e["stage"], e["status"]) for e in stage_events] == [
        ("classify", "start"),
        ("classify", "done"),
    ]
    # Assert absence of the whole class of later-stage events, not just that one particular name is
    # missing -- a CLARIFY response must never emit ANY of retrieve/generate/verify, since none of
    # those stages ran (see app/pipeline.py's module docstring).
    assert not any(e["stage"] in {"retrieve", "generate", "verify"} for e in stage_events)

    result_events = [e for e in events if e["event"] == "result"]
    assert len(result_events) == 1
    assert result_events[0]["response"]["response_type"] == ResponseType.CLARIFY.value


def test_query_stream_reports_an_error_event_and_logs_the_traceback_on_failure(stub_client, caplog):
    class ExplodingLLM(StubLLM):
        async def generate(self, system: str, user: str) -> str:
            raise RuntimeError("synthetic failure for the /query/stream error-path test")

    app.state.llm = ExplodingLLM()

    with stub_client.stream(
        "POST", "/query/stream", json={"question": "How long is the STEM OPT extension?"}
    ) as response:
        assert response.status_code == 200
        events = _read_sse_events(response)

    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) == 1, f"expected exactly one error event, got: {events}"
    assert "RuntimeError" in error_events[0]["detail"]
    assert "synthetic failure" in error_events[0]["detail"]
    assert not any(e["event"] == "result" for e in events)
    assert any(
        "POST /query/stream failed" in record.message for record in caplog.records
    ), "expected the full traceback to be logged, the same way POST /query's handler logs one"


def test_sources_status_reports_a_real_source_count_and_freshness_fields(stub_client):
    response = stub_client.get("/sources/status")
    assert response.status_code == 200
    body = response.json()
    assert body["source_count"] > 0
    assert body["oldest_verified_at"] is not None
    assert body["newest_verified_at"] is not None
    assert body["freshness_state"] in {"current", "recent", "stale", "unknown"}
    # Phase 7 fields: present, well-typed, and internally consistent regardless of which corpus is
    # currently loaded (a freshly ingested corpus has no broken sources at all).
    assert isinstance(body["broken_source_count"], int)
    assert isinstance(body["broken_sources"], list)
    assert body["broken_source_count"] == len(body["broken_sources"])


async def test_sources_status_reports_broken_sources_correctly():
    """DoD 6: GET /sources/status reports broken_source_count/broken_sources correctly, decided
    entirely by app.guardrails.freshness.source_health_state (never recomputed by this test or by
    the endpoint's SQL) -- a source with 3 consecutive failures shows up as broken with its own
    error/status/count carried through; an untouched, healthy source does not. Runs against a
    scratch database (never the live, fully-ingested corpus -- same guard test_freshness.py's
    DB-writing tests use), and calls the endpoint's own handler function directly rather than
    through TestClient/lifespan, so this test owns exactly one connection pool.
    """
    database_url = os.environ.get("DATABASE_URL", get_settings().DATABASE_URL)
    guard_conn = await psycopg.AsyncConnection.connect(database_url)
    try:
        await _refuse_if_target_is_the_fully_ingested_real_corpus(guard_conn, database_url)
    finally:
        await guard_conn.close()

    ok_url = "https://example.gov/status-endpoint-ok"
    broken_url = "https://example.gov/status-endpoint-broken"

    write_conn = await psycopg.AsyncConnection.connect(database_url)
    await register_vector_async(write_conn)

    pool = make_pool(database_url)
    await pool.open()
    app.state.pool = pool
    app.state.settings = Settings(LLM_PROVIDER="stub", EMBED_PROVIDER="stub")

    try:
        now = datetime.now(UTC)
        await _insert_test_row(write_conn, ok_url, fetched_at=now)
        await _insert_test_row(write_conn, broken_url, fetched_at=now)

        async with write_conn.cursor() as cur:
            await cur.execute(
                "UPDATE sources SET consecutive_failures = 3, status = 'fetch_failed', "
                "last_error = %s, last_http_status = %s WHERE source_url = %s",
                ("simulated failure", 503, broken_url),
            )
        await write_conn.commit()

        status = await sources_status()

        broken_by_url = {b.source_url: b for b in status.broken_sources}
        assert broken_url in broken_by_url
        assert ok_url not in broken_by_url
        assert broken_by_url[broken_url].status == "fetch_failed"
        assert broken_by_url[broken_url].consecutive_failures == 3
        assert broken_by_url[broken_url].last_error == "simulated failure"
        assert broken_by_url[broken_url].last_http_status == 503
        assert status.broken_source_count == len(status.broken_sources)
    finally:
        await _delete_test_rows(write_conn, ok_url, broken_url)
        await write_conn.close()
        await pool.close()


def _true_min_max_last_verified_at(database_url: str) -> tuple[datetime, datetime]:
    # Phase 7: last_verified_at moved off `documents` (one identical copy per chunk) onto
    # `sources` (one row per source) -- see infra/sql/init.sql.
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT min(last_verified_at), max(last_verified_at) FROM sources")
            true_min, true_max = cur.fetchone()
    return true_min, true_max


def test_sources_status_oldest_verified_at_is_the_true_minimum_not_the_maximum(stub_client):
    """`oldest_verified_at` must be the weakest link -- the true `min(last_verified_at)` across
    EVERY row in `documents` -- never the newest. This is the exact regression the per-source
    aggregation query in app/main.py fixes: a plain `max()` would let one freshly re-crawled source
    disguise a corpus where other sources went unchecked for a week.
    """
    response = stub_client.get("/sources/status")
    body = response.json()

    settings = get_settings()
    database_url = os.environ.get("DATABASE_URL", settings.DATABASE_URL)
    true_min, true_max = _true_min_max_last_verified_at(database_url)

    reported_oldest = datetime.fromisoformat(body["oldest_verified_at"])
    assert reported_oldest == true_min, (
        f"oldest_verified_at {reported_oldest} does not equal the true min(last_verified_at) "
        f"{true_min} across all rows in documents"
    )
    if true_min == true_max:
        # Every row in the currently loaded corpus was verified at exactly the same instant (it
        # was ingested once and never re-crawled since), so min and max are the same value and
        # there is no "not the max" half of this invariant to exercise -- a fact about this
        # corpus's current state, not a gap in this test. See the real regression case this test
        # guards, in test_sources_status_current_state_implies_no_stale_source_below.
        pass
    else:
        assert reported_oldest != true_max, (
            "oldest_verified_at equals the true max(last_verified_at) -- it must report the "
            "weakest link (the minimum), never the newest"
        )


def test_sources_status_current_state_implies_no_stale_source_and_a_true_recent_minimum(
    stub_client,
):
    """THE INVARIANT: it must be impossible for the endpoint to report freshness_state=="current"
    while any source is older than 24 hours. Checked against whatever corpus is actually loaded --
    this is an implication (if the endpoint claims "current", the two facts below must both hold),
    not an assertion that the corpus must currently be in the "current" band; the true minimum is
    read directly from `documents`, never trusted from the endpoint's own reported number.
    """
    response = stub_client.get("/sources/status")
    body = response.json()

    settings = get_settings()
    database_url = os.environ.get("DATABASE_URL", settings.DATABASE_URL)
    true_min, _true_max = _true_min_max_last_verified_at(database_url)

    if body["freshness_state"] == "current":
        assert body["stale_source_count"] == 0, (
            "freshness_state == 'current' but stale_source_count is nonzero -- a source with an "
            "old oldest-verified chunk cannot coexist with an overall 'current' verdict"
        )
        assert true_min is not None
        true_age = datetime.now(UTC) - true_min
        assert true_age <= timedelta(hours=24), (
            f"freshness_state == 'current' but the true min(last_verified_at) {true_min} across "
            f"all rows in documents is {true_age} old -- more than 24 hours, which the header "
            "must never claim as current"
        )


# --- Pure-function tests: app/guardrails/freshness.py::sources_freshness_state. No DB, no clock of
# --- its own -- a fixed `now` throughout so these never depend on the wall clock. ---

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def test_sources_freshness_state_none_oldest_verified_at_is_unknown():
    assert sources_freshness_state(None, now=_NOW) == ("unknown", None)


@pytest.mark.parametrize(
    ("age", "expected_state"),
    [
        (timedelta(hours=0), "current"),
        (timedelta(hours=1), "current"),
        (timedelta(hours=24), "current"),  # exact boundary: <= 24h is still "current"
        (timedelta(hours=24, seconds=1), "recent"),  # just past the 24h boundary
        (timedelta(hours=100), "recent"),
        (timedelta(hours=168), "recent"),  # exact boundary: <= 168h (7 days) is still "recent"
        (timedelta(hours=168, seconds=1), "stale"),  # just past the 168h boundary
        (timedelta(days=30), "stale"),
    ],
)
def test_sources_freshness_state_bands_and_boundaries(age, expected_state):
    oldest_verified_at = _NOW - age
    state, age_hours = sources_freshness_state(oldest_verified_at, now=_NOW)
    assert state == expected_state
    assert age_hours == pytest.approx(age.total_seconds() / 3600)


def test_sources_freshness_state_a_future_timestamp_is_current_not_a_crash():
    """A negative age (oldest_verified_at in the future relative to `now` -- clock skew) must be
    treated as "current", never raise."""
    future = _NOW + timedelta(hours=2)
    state, age_hours = sources_freshness_state(future, now=_NOW)
    assert state == "current"
    assert age_hours < 0


# --- The on_event default guard: answer_question(on_event=None) (what POST /query passes) must
# --- still return the same response_type/citations it always did -- see app/pipeline.py's module
# --- docstring, "nothing about the pipeline's own control flow or return value changes". ---


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


@pytest.fixture
def llm():
    return StubLLM()


@pytest.fixture
def settings():
    return Settings(LLM_PROVIDER="stub", EMBED_PROVIDER="stub", NO_ANSWER_MAX_DISTANCE=2.0)


async def test_answer_question_default_on_event_matches_a_no_op_on_event(
    pool, embedder, llm, settings
):
    question = "How long is the STEM OPT extension?"
    baseline = await answer_question(
        question, pool=pool, embedder=embedder, llm=llm, settings=settings
    )

    observed_events = []

    async def on_event(event: dict) -> None:
        observed_events.append(event)

    with_on_event = await answer_question(
        question, pool=pool, embedder=embedder, llm=llm, settings=settings, on_event=on_event
    )

    assert with_on_event.response_type == baseline.response_type
    assert with_on_event.citations == baseline.citations
    assert with_on_event.refusal_reason == baseline.refusal_reason


# =================================================================================================
# Phase 8 round 4: GET /usage's distinct_sessions must be REAL behind the Go gateway, not stuck at
# 1 forever. The gateway (services/gateway/internal/middleware/session.go) sets
# X-Office-Hours-Session-Hash on every request it forwards -- an HMAC-SHA256 hex digest of the
# ORIGINAL caller's own address, computed at the gateway, never the raw address itself. app/main.py
# ::_client_session_hash must use that header's value directly when it is present and looks like a
# real hash, and fall back to hashing THIS process's own peer address (TestClient's fixed one, in
# these tests) exactly as it always has when the header is absent or malformed.
# =================================================================================================


def test_distinct_sessions_counts_real_distinct_clients_via_the_forwarded_session_header(
    stub_client,
):
    # Fresh, random 64-character hex hashes per test run (never "a" * 64 / "b" * 64 -- a repeated,
    # fixed value would already be recorded in usage_sessions from an earlier run against the same
    # persistent database, the same reason tests/test_usage.py's own
    # test_record_query_dedupes_the_same_session_hash uses a fresh uuid4 rather than a fixed
    # string), so "distinct_sessions grew by exactly 2" is correct on every run, not just the
    # first.
    hash_a = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    hash_b = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    question = {"question": "How long is the STEM OPT extension?"}

    before = stub_client.get("/usage").json()

    stub_client.post("/query", json=question, headers={"X-Office-Hours-Session-Hash": hash_a})
    stub_client.post("/query", json=question, headers={"X-Office-Hours-Session-Hash": hash_a})
    stub_client.post("/query", json=question, headers={"X-Office-Hours-Session-Hash": hash_b})

    after = stub_client.get("/usage").json()
    assert after["total_queries"] == before["total_queries"] + 3
    # hash_a recorded twice, hash_b once -- exactly two DISTINCT sessions, proving distinct_sessions
    # reflects the forwarded header rather than TestClient's own single, fixed peer address (which
    # would otherwise collapse every request behind the gateway into one session forever).
    assert after["distinct_sessions"] == before["distinct_sessions"] + 2


def test_malformed_session_hash_header_falls_back_to_the_peer_address_hash(stub_client):
    """A header value that is not a real 64-character hex digest (a bug in, or a caller bypassing,
    the gateway) must never be trusted verbatim -- it falls back to hashing this request's own peer
    address instead, exactly as if the header were absent. Two DIFFERENT malformed values from the
    SAME TestClient (a single, fixed peer address) must therefore still count as only ONE distinct
    session, not two -- if the code wrongly used the malformed header text as-is, this would be 2.

    A FRESH salt (a random uuid4, the same convention tests/test_usage.py's own
    test_record_query_dedupes_the_same_session_hash uses) is set on app.state.settings before
    issuing any request: TestClient's peer address is a FIXED constant ("testclient"), so without a
    fresh salt the fallback hash this test exercises would already have been recorded by an earlier
    test (or an earlier run against the same persistent usage_sessions table), and
    distinct_sessions would not visibly grow at all -- not because the code is wrong, but because
    usage_sessions accumulates every distinct hash FOREVER (see app/usage.py's own docstring). A
    fresh salt makes THIS test's own fallback hash provably never-before-seen.
    """
    app.state.settings = Settings(
        LLM_PROVIDER="stub",
        EMBED_PROVIDER="stub",
        NO_ANSWER_MAX_DISTANCE=2.0,
        SESSION_HASH_SALT=str(uuid.uuid4()),
    )
    question = {"question": "How long is the STEM OPT extension?"}
    before = stub_client.get("/usage").json()

    stub_client.post(
        "/query", json=question, headers={"X-Office-Hours-Session-Hash": "not-a-real-hash"}
    )
    stub_client.post(
        "/query", json=question, headers={"X-Office-Hours-Session-Hash": "also-not-a-real-hash"}
    )

    after = stub_client.get("/usage").json()
    assert after["total_queries"] == before["total_queries"] + 2
    assert after["distinct_sessions"] == before["distinct_sessions"] + 1


def test_absent_session_hash_header_falls_back_to_the_peer_address_hash_unchanged(stub_client):
    """No header at all (a direct caller, or any client that does not sit behind the gateway) must
    behave exactly as it always has: hashed from this request's own peer address. Proved the same
    way as the malformed-header case -- two requests with no header, from the same fixed
    TestClient peer, count as one distinct session. See the malformed-header test above for why a
    fresh salt is required for this assertion to be meaningful.
    """
    app.state.settings = Settings(
        LLM_PROVIDER="stub",
        EMBED_PROVIDER="stub",
        NO_ANSWER_MAX_DISTANCE=2.0,
        SESSION_HASH_SALT=str(uuid.uuid4()),
    )
    question = {"question": "How long is the STEM OPT extension?"}
    before = stub_client.get("/usage").json()

    stub_client.post("/query", json=question)
    stub_client.post("/query", json=question)

    after = stub_client.get("/usage").json()
    assert after["total_queries"] == before["total_queries"] + 2
    assert after["distinct_sessions"] == before["distinct_sessions"] + 1
