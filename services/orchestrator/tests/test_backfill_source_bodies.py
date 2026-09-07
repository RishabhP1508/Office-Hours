"""Tests for app/backfill_source_bodies.py -- the one-time backfill that populates
`sources.last_indexed_body`/`rule_effective_date` for every source ingested before those columns
existed (docs/adr/0014-stateless-recrawl-diff.md). No langgraph needed: this module imports only
app.config/app.ingest and stdlib.

`database_url`/`conn` below are the same fixture SHAPE test_freshness.py defines (including its
guard against running against the real, fully-ingested corpus), redefined here rather than
imported: a pytest fixture and a test function's same-named parameter live in the same module
namespace, so importing a fixture under the exact name a local test function also uses as a
parameter reads, to a linter, as the import being shadowed/unused in every test -- reusing the
underlying guard function (`_refuse_if_target_is_the_fully_ingested_real_corpus`, imported directly
below) is what actually avoids a second copy of that safety check; only the thin fixture wiring
around it is duplicated.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from test_freshness import (
    _delete_test_rows,
    _insert_test_row,
    _refuse_if_target_is_the_fully_ingested_real_corpus,
)

from app.backfill_source_bodies import backfill_source_bodies
from app.config import get_settings
from app.ingest import build_frontmatter, render_snapshot


@pytest.fixture
def database_url() -> str:
    return os.environ.get("DATABASE_URL", get_settings().DATABASE_URL)


@pytest.fixture
async def conn(database_url):
    connection = await psycopg.AsyncConnection.connect(database_url)
    await register_vector_async(connection)
    try:
        await _refuse_if_target_is_the_fully_ingested_real_corpus(connection, database_url)
        yield connection
    finally:
        await connection.close()


def _write_snapshot(
    raw_dir: Path, *, source_url: str, body: str, rule_effective_date: str | None = None
) -> Path:
    frontmatter = build_frontmatter(
        {},
        {
            "source_url": source_url,
            "title": "Test Page",
            "fetched_at": "2026-01-01",
            "page_last_updated": "2026-01-01",
            "topic": "freshness_test",
        },
    )
    if rule_effective_date:
        frontmatter["rule_effective_date"] = rule_effective_date
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{source_url.rsplit('/', 1)[-1]}.md"
    path.write_text(render_snapshot(frontmatter, body), encoding="utf-8")
    return path


async def test_backfill_populates_null_body_and_annotation_from_local_snapshot(
    tmp_path, conn, database_url
):
    source_url = "https://example.gov/backfill-test-basic"
    body = "# Test Page\n\n## Section\n\nThe extension is 24 months.\n"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    # Deliberately no last_indexed_body -- the exact post-migration, pre-backfill state.
    await _insert_test_row(
        conn, source_url, fetched_at=old_time, page_last_updated=date(2026, 1, 1)
    )
    _write_snapshot(tmp_path, source_url=source_url, body=body, rule_effective_date="2026-09-15")

    # sources_rows_still_null_after counts NULLs across the WHOLE `sources` table, by design (see
    # backfill_source_bodies's own docstring: it is meant to answer "did the human miss a source"
    # for the database as a whole, not just for this call's own raw_dir). That makes an absolute
    # "== 0" assertion fragile against any OTHER null row a different test or process happens to
    # leave behind in a shared/dirty database -- scope this to the DELTA this call itself is
    # responsible for instead: exactly one fewer NULL row than there was a moment ago.
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM sources WHERE last_indexed_body IS NULL")
        (null_count_before,) = await cur.fetchone()

    counts = await backfill_source_bodies(conn, tmp_path)

    assert counts["updated"] == 1
    assert counts["already_populated"] == 0
    assert counts["snapshots_without_a_sources_row"] == 0
    assert counts["sources_rows_still_null_after"] == null_count_before - 1

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT last_indexed_body, rule_effective_date FROM sources WHERE source_url = %s",
            (source_url,),
        )
        row = await cur.fetchone()
    assert row["last_indexed_body"] == body
    assert row["rule_effective_date"] == date(2026, 9, 15)

    await _delete_test_rows(conn, source_url)


async def test_backfill_is_idempotent_and_never_overwrites_an_already_populated_body(
    tmp_path, conn, database_url
):
    """Safe to re-run: a SECOND call, with the snapshot file now claiming DIFFERENT content, must
    change nothing -- the first call already populated last_indexed_body, and the WHERE
    last_indexed_body IS NULL guard is what makes a re-run a no-op rather than a clobber.
    """
    source_url = "https://example.gov/backfill-test-idempotent"
    body = "# Test Page\n\n## Section\n\nThe extension is 24 months.\n"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    await _insert_test_row(
        conn, source_url, fetched_at=old_time, page_last_updated=date(2026, 1, 1)
    )
    _write_snapshot(tmp_path, source_url=source_url, body=body)

    first = await backfill_source_bodies(conn, tmp_path)
    assert first["updated"] == 1

    # Simulate a stale/different local snapshot file before the second run -- if the guard were
    # missing, this would silently clobber the real body the first run (or a real reindex since)
    # already committed.
    _write_snapshot(
        tmp_path, source_url=source_url, body="# Test Page\n\nSOMETHING ELSE ENTIRELY\n"
    )

    second = await backfill_source_bodies(conn, tmp_path)
    assert second["updated"] == 0
    assert second["already_populated"] == 1

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT last_indexed_body FROM sources WHERE source_url = %s", (source_url,)
        )
        row = await cur.fetchone()
    assert (
        row["last_indexed_body"] == body
    ), "a re-run must never overwrite an already-populated body"

    await _delete_test_rows(conn, source_url)


async def test_backfill_dry_run_writes_nothing(tmp_path, conn, database_url):
    source_url = "https://example.gov/backfill-test-dry-run"
    body = "# Test Page\n\n## Section\n\nThe extension is 24 months.\n"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    await _insert_test_row(
        conn, source_url, fetched_at=old_time, page_last_updated=date(2026, 1, 1)
    )
    _write_snapshot(tmp_path, source_url=source_url, body=body)

    counts = await backfill_source_bodies(conn, tmp_path, dry_run=True)
    assert counts["updated"] == 1  # reported as "would update"

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT last_indexed_body FROM sources WHERE source_url = %s", (source_url,)
        )
        row = await cur.fetchone()
    assert row["last_indexed_body"] is None, "--dry-run must write nothing"

    await _delete_test_rows(conn, source_url)


async def test_backfill_flags_a_snapshot_with_no_matching_sources_row(tmp_path, conn, database_url):
    """A snapshot on disk for a source_url that was never ingested at all (no `sources` row yet) --
    nothing to backfill, flagged rather than silently skipped or errored.
    """
    source_url = "https://example.gov/backfill-test-orphan-snapshot"
    _write_snapshot(tmp_path, source_url=source_url, body="# Test Page\n\nSome content.\n")

    counts = await backfill_source_bodies(conn, tmp_path)

    assert counts["snapshots_without_a_sources_row"] >= 1


async def test_backfill_write_is_durable_across_a_separate_connection(tmp_path, conn, database_url):
    """The write must be COMMITTED, not merely visible to the same session that made it: a bare
    `execute()` with no explicit transaction wrapping opens an ambient transaction that a
    connection close() rolls back rather than commits, which would make the backfill look like it
    worked (this same `conn` would see its own uncommitted write) while a fresh connection -- the
    real production case, where `python -m app.backfill_source_bodies` opens one connection, does
    the work, and exits -- would see nothing at all. Opens a SECOND, independent connection to
    verify, rather than trusting `conn`'s own view of what it just wrote.
    """
    source_url = "https://example.gov/backfill-test-durable-commit"
    body = "# Test Page\n\n## Section\n\nThe extension is 24 months.\n"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    await _insert_test_row(
        conn, source_url, fetched_at=old_time, page_last_updated=date(2026, 1, 1)
    )
    _write_snapshot(tmp_path, source_url=source_url, body=body)

    counts = await backfill_source_bodies(conn, tmp_path)
    assert counts["updated"] == 1

    other_conn = await psycopg.AsyncConnection.connect(database_url)
    await register_vector_async(other_conn)
    try:
        async with other_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT last_indexed_body FROM sources WHERE source_url = %s", (source_url,)
            )
            row = await cur.fetchone()
        assert row is not None
        assert row["last_indexed_body"] == body, (
            "the backfill's write must be committed, not merely visible within the same session "
            "that made it"
        )
    finally:
        await other_conn.close()

    await _delete_test_rows(conn, source_url)
