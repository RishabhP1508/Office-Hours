"""Tests for app/backfill_source_bodies.py -- the one-time backfill that populates
`sources.last_indexed_body` for every source ingested before that column existed
(docs/adr/0014-stateless-recrawl-diff.md). No langgraph needed: this module imports only
app.config/app.ingest and stdlib.

TWO CONNECTIONS, on purpose. The tool is SYNCHRONOUS as it ships, so `sync_conn` (a real
`psycopg.Connection`) is what it is exercised through. The `conn` fixture stays ASYNC because the
arrange/assert/cleanup helpers are shared verbatim with test_freshness.py, whose subject
(app/recrawl.py) is genuinely async. Both fixtures run the same corpus guard, via the one decision
function they share.

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
    refuse_if_manifest_fully_present,
)

from app.backfill_source_bodies import backfill_source_bodies
from app.config import get_settings
from app.ingest import build_frontmatter, render_snapshot


@pytest.fixture
def database_url() -> str:
    return os.environ.get("DATABASE_URL", get_settings().DATABASE_URL)


@pytest.fixture
async def conn(database_url):
    """ASYNC, and only for this file's SCAFFOLDING: `_insert_test_row`/`_delete_test_rows` are
    shared with test_freshness.py (which tests app/recrawl.py, genuinely async) and reused here
    unchanged rather than forked -- `_insert_test_row` alone is ~50 lines of SQL including an
    `embedding`, so a second sync copy would be worse than the problem it solved.
    """
    connection = await psycopg.AsyncConnection.connect(database_url)
    await register_vector_async(connection)
    try:
        await _refuse_if_target_is_the_fully_ingested_real_corpus(connection, database_url)
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def sync_conn(database_url):
    """SYNCHRONOUS, and this is the one under test. `backfill_source_bodies` is sync as it ships
    (see its own docstring for why), so it is exercised through a real `psycopg.Connection` rather
    than through whatever the scaffolding happens to use.

    Guarded by the SAME rule as `conn` above, via the decision function both share, so there is no
    path into this file that writes to the real corpus without the check. No `register_vector`: the
    backfill touches no vector column, and the `embedding` the scaffolding writes goes through the
    async connection.
    """
    connection = psycopg.connect(database_url)
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT DISTINCT source_url FROM documents")
            present = {row[0] for row in cur.fetchall()}
        connection.rollback()
        refuse_if_manifest_fully_present(present, database_url)
        yield connection
    finally:
        connection.close()


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


async def test_backfill_populates_null_body_from_local_snapshot(
    tmp_path, conn, sync_conn, database_url
):
    """Renamed TWICE on 19 September 2026, and the second rename is the interesting one.

    It was `..._populates_null_body_and_annotation_from_local_snapshot` and asserted the backfill
    copied `rule_effective_date` out of the snapshot frontmatter. That write was removed as the
    first piece of Option B, so the assertion was INVERTED to `row["rule_effective_date"] is None`:
    the snapshot still carries the value, so the tool was handed what it used to copy and shown to
    ignore it.

    That inverted assertion is now GONE, and not because it was inconvenient. Option B also removed
    `sources.rule_effective_date` from infra/sql/init.sql's declaration, so the CI database built
    from that file has no such column and `SELECT ... rule_effective_date FROM sources` raises
    UndefinedColumn before any assertion runs. **The assertion did not become wrong. Its subject
    stopped existing.** There is no longer a value that could be NULL or not-NULL, so this is not a
    claim a database test can make at all any more.

    The claim itself did not move, because it was never only here:
    `test_backfill_writes_last_indexed_body_and_never_rule_effective_date` below asserts, at the
    STATEMENT level against a recording connection, that the UPDATE names no column in
    `_NEVER_WRITTEN`, and `test_the_never_written_check_would_actually_catch_a_violation` proves
    that predicate trips on the real pre-19-September statement. Both run with no database. Putting
    the write back still fails the suite; it fails there instead of here, which is the only place it
    can still be checked.

    What this test proves now is the half that IS a database fact: a source whose
    `last_indexed_body` is NULL gets the snapshot's body written into it.
    """
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

    counts = backfill_source_bodies(sync_conn, tmp_path)

    assert counts["updated"] == 1
    assert counts["already_populated"] == 0
    assert counts["snapshots_without_a_sources_row"] == 0
    assert counts["sources_rows_still_null_after"] == null_count_before - 1

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT last_indexed_body FROM sources WHERE source_url = %s",
            (source_url,),
        )
        row = await cur.fetchone()
    assert row["last_indexed_body"] == body
    # `rule_effective_date` is deliberately NOT selected or asserted here; the column is not in
    # infra/sql/init.sql's declaration any more, so selecting it raises UndefinedColumn against the
    # CI database rather than failing an assertion. See this test's docstring, and
    # test_backfill_writes_last_indexed_body_and_never_rule_effective_date for where the claim
    # that the backfill never writes it is actually made and controlled.

    await _delete_test_rows(conn, source_url)


async def test_backfill_is_idempotent_and_never_overwrites_an_already_populated_body(
    tmp_path, conn, sync_conn, database_url
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

    first = backfill_source_bodies(sync_conn, tmp_path)
    assert first["updated"] == 1

    # Simulate a stale/different local snapshot file before the second run -- if the guard were
    # missing, this would silently clobber the real body the first run (or a real reindex since)
    # already committed.
    _write_snapshot(
        tmp_path, source_url=source_url, body="# Test Page\n\nSOMETHING ELSE ENTIRELY\n"
    )

    second = backfill_source_bodies(sync_conn, tmp_path)
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


async def test_backfill_dry_run_writes_nothing(tmp_path, conn, sync_conn, database_url):
    source_url = "https://example.gov/backfill-test-dry-run"
    body = "# Test Page\n\n## Section\n\nThe extension is 24 months.\n"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    await _insert_test_row(
        conn, source_url, fetched_at=old_time, page_last_updated=date(2026, 1, 1)
    )
    _write_snapshot(tmp_path, source_url=source_url, body=body)

    counts = backfill_source_bodies(sync_conn, tmp_path, dry_run=True)
    assert counts["updated"] == 1  # reported as "would update"

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT last_indexed_body FROM sources WHERE source_url = %s", (source_url,)
        )
        row = await cur.fetchone()
    assert row["last_indexed_body"] is None, "--dry-run must write nothing"

    await _delete_test_rows(conn, source_url)


async def test_backfill_flags_a_snapshot_with_no_matching_sources_row(
    tmp_path, conn, sync_conn, database_url
):
    """A snapshot on disk for a source_url that was never ingested at all (no `sources` row yet) --
    nothing to backfill, flagged rather than silently skipped or errored.
    """
    source_url = "https://example.gov/backfill-test-orphan-snapshot"
    _write_snapshot(tmp_path, source_url=source_url, body="# Test Page\n\nSome content.\n")

    counts = backfill_source_bodies(sync_conn, tmp_path)

    assert counts["snapshots_without_a_sources_row"] >= 1


async def test_backfill_write_is_durable_across_a_separate_connection(
    tmp_path, conn, sync_conn, database_url
):
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

    counts = backfill_source_bodies(sync_conn, tmp_path)
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


# ---------------------------------------------------------------------------------------------
# NO DATABASE NEEDED below this line. Everything above uses the `conn`/`sync_conn` fixtures and
# therefore cannot run on Windows (psycopg refuses async mode on the ProactorEventLoop, and there
# is usually no local Postgres). The statement-level regression below drives the real
# `backfill_source_bodies` through a fake connection instead, so the one property that most needs
# watching is checkable on any machine -- see tests/test_sync_annotations.py for the same pattern
# and for what a fake connection can and cannot establish.
# ---------------------------------------------------------------------------------------------

# Columns backfill_source_bodies must never write. `rule_effective_date` heads this list because it
# WAS written until 19 September 2026, when that half of the UPDATE was removed as the first piece
# of Option B. Its presence here is what proves the removal on every run rather than leaving it as
# something someone remembers having done.
_NEVER_WRITTEN = (
    "rule_effective_date",
    "fetched_at",
    "last_verified_at",
    "last_changed_at",
    "last_success_at",
    "change_count",
    "consecutive_failures",
    "status",
    "content",
    "embedding",
)


class _RecordingCursor:
    def __init__(self, state, log):
        self.state, self.log, self.rowcount, self._row, self._rows = state, log, 0, None, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.log.append(flat)
        if flat.startswith("SELECT last_indexed_body FROM sources"):
            url = params[0]
            self._row = (self.state.get(url),) if url in self.state else None
        elif flat.startswith("SELECT source_url FROM sources WHERE last_indexed_body IS NULL"):
            self._rows = [(u,) for u, b in sorted(self.state.items()) if b is None]
        elif flat.startswith("UPDATE sources"):
            self.state[params[-1]] = params[0]
            self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {flat}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _RecordingConn:
    def __init__(self, sources):
        self.state, self.log = dict(sources), []

    def transaction(self):
        return _RecordingTxn()

    def cursor(self):
        return _RecordingCursor(self.state, self.log)

    @property
    def writes(self):
        return [s for s in self.log if s.startswith(("UPDATE", "INSERT", "DELETE"))]


class _RecordingTxn:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_backfill_writes_last_indexed_body_and_never_rule_effective_date(tmp_path):
    """REGRESSION, no database required. The UPDATE used to write `rule_effective_date` as well;
    that half was removed so the backfill can run against a schema (Option B's) that will never
    have the column. The DB-backed test above asserts the same property through a real row, but it
    cannot run on Windows, so this asserts it at the statement level where anyone can check it.
    """
    source_url = "https://example.gov/backfill-statement-level"
    _write_snapshot(
        tmp_path, source_url=source_url, body="# Page\n\nBody.\n", rule_effective_date="2026-09-15"
    )
    conn = _RecordingConn({source_url: None})

    counts = backfill_source_bodies(conn, tmp_path)

    assert counts["updated"] == 1
    assert len(conn.writes) == 1
    statement = conn.writes[0]
    assert "last_indexed_body = %s" in statement
    for column in _NEVER_WRITTEN:
        assert column not in statement, f"{column!r} written by: {statement}"


def test_the_never_written_check_would_actually_catch_a_violation():
    """The control. A forbidden-column assertion pointed at nothing passes forever; this feeds the
    same predicate the pre-19-September statement and confirms it trips on it.
    """
    old_form = (
        "UPDATE sources SET last_indexed_body = %s, rule_effective_date = %s "
        "WHERE source_url = %s AND last_indexed_body IS NULL"
    )
    assert [c for c in _NEVER_WRITTEN if c in old_form] == ["rule_effective_date"]

    current_form = (
        "UPDATE sources SET last_indexed_body = %s "
        "WHERE source_url = %s AND last_indexed_body IS NULL"
    )
    assert [c for c in _NEVER_WRITTEN if c in current_form] == []


def test_dry_run_predicts_which_sources_stay_unbacked_rather_than_reporting_zero(tmp_path):
    """`--dry-run` used to return `sources_rows_still_null_after: 0` unconditionally, because
    nothing had been written: a clean bill of health for work not done. It now subtracts the rows
    this pass would fill and NAMES what is left.
    """
    covered = "https://example.gov/has-a-snapshot"
    orphan = "https://example.gov/no-snapshot-on-disk"
    _write_snapshot(tmp_path, source_url=covered, body="# Page\n\nBody.\n")
    conn = _RecordingConn({covered: None, orphan: None})

    counts = backfill_source_bodies(conn, tmp_path, dry_run=True)

    assert conn.writes == []
    assert counts["updated"] == 1
    assert counts["sources_rows_still_null_after"] == 1
    assert counts["unbacked_source_urls"] == [orphan]
