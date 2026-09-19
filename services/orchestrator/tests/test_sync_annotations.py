"""Tests for app/sync_annotations.py -- the tool that pushes curator annotations from
data/sources/sources.yaml into `documents`.

NO DATABASE REQUIRED. Every test here drives the real `sync_annotations` through a fake connection
that records each statement and transaction boundary, so they pass on any machine including
Windows, where the DB-backed tests in this suite cannot run at all (psycopg refuses async mode on
the ProactorEventLoop, and there is usually no local Postgres). With test_check_schema.py these are
the only tests in this project a developer on Windows can execute locally. Worth knowing before
pushing a branch to ask CI something these answer in under a second.

WHAT A FAKE CONNECTION CAN AND CANNOT ESTABLISH. It is strong evidence about WHICH statements are
issued, in what order, inside which transaction, against which table and columns -- which is exactly
what matters for a tool that writes to production and whose blast radius is documented as a fixed
list. It is no evidence at all about how Postgres responds: `cur.rowcount` semantics and the real
commit boundary of `conn.transaction()` are stubbed here, not observed. Those two were measured for
app/backfill_source_bodies.py by its real run against production on 19 September; for this tool they
remain unobserved, and a passing run of this file does not change that.

The fake RAISES on any statement shape it does not model, rather than ignoring it. That is
load-bearing: when the tool's read moved off `sources` onto `documents`, eight tests failed loudly
instead of passing against a statement nothing had checked.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
import yaml

from app.sync_annotations import manifest_annotation, sync_annotations

FAQ = "https://studyinthestates.dhs.gov/final-rule-faq"
QUICK = "https://studyinthestates.dhs.gov/final-rule-quick"

# Columns app/sync_annotations.py's docstring promises are NEVER written. `documents.content` and
# `embedding` are here because writing either would mean re-indexing, which is the whole thing this
# tool exists to avoid.
NEVER_WRITTEN = (
    "fetched_at",
    "last_verified_at",
    "last_changed_at",
    "last_success_at",
    "change_count",
    "consecutive_failures",
    "last_error",
    "last_http_status",
    "status",
    "last_indexed_body",
    "resolved_url",
    "page_last_updated",
    "content",
    "embedding",
    "section_heading",
    "heading_level",
)


def _written_table(statement: str) -> str | None:
    """The table an INSERT/UPDATE/DELETE targets, or None if the statement is not a write."""
    match = re.match(r"(?:UPDATE|INSERT INTO|DELETE FROM)\s+(\w+)", statement, re.IGNORECASE)
    return match.group(1) if match else None


class FakeCursor:
    def __init__(self, docs, log):
        self.docs, self.log, self.rowcount = docs, log, 0
        self._row = None
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.log.append((flat, params))
        if flat.startswith("SELECT DISTINCT rule_effective_date FROM documents"):
            # dict.fromkeys preserves order while de-duplicating, like DISTINCT on one column.
            self._rows = [(v,) for v in dict.fromkeys(self.docs.get(params[0], []))]
        elif flat.startswith("SELECT count(*) FROM documents"):
            self._row = (len(self.docs.get(params[0], [])),)
        elif flat.startswith("UPDATE documents"):
            url = params[1]
            chunks = self.docs.get(url, [])
            self.docs[url] = [params[0]] * len(chunks)
            self.rowcount = len(chunks)
        else:
            # Deliberately fatal. A statement shape this fake does not model is a statement nothing
            # here has checked, and silently tolerating it is how a test suite keeps passing while
            # the tool underneath it changes. `UPDATE sources` lands here on purpose.
            raise AssertionError(f"unexpected SQL: {flat}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeTxn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        self.log.append(("BEGIN", None))
        return self

    def __exit__(self, *_):
        self.log.append(("COMMIT", None))
        return False


class FakeConn:
    """Records every statement and transaction boundary `sync_annotations` issues.

    `docs` maps a source_url to its PER-CHUNK `rule_effective_date` values, which is how the real
    table is shaped: one row per chunk. A source absent from this mapping has no chunks at all.
    """

    def __init__(self, docs: dict[str, list[date | None]]):
        self.docs = {k: list(v) for k, v in docs.items()}
        self.log: list[tuple[str, object]] = []

    def transaction(self):
        return FakeTxn(self.log)

    def cursor(self):
        return FakeCursor(self.docs, self.log)

    @property
    def statements(self) -> list[str]:
        return [s for s, _ in self.log if s not in {"BEGIN", "COMMIT"}]

    @property
    def writes(self) -> list[str]:
        return [s for s in self.statements if _written_table(s)]

    @property
    def transactions(self) -> int:
        return sum(1 for s, _ in self.log if s == "BEGIN")


def entry(url: str, rule_effective_date: date | None = None) -> dict:
    ann = {"rule_effective_date": rule_effective_date} if rule_effective_date else {}
    return {"url": url, "topic": "fixed_admission", "annotations": ann}


# ---------------------------------------------------------------------------------------------
# THE REGRESSION. This is why this file exists rather than only the scenarios below.
# ---------------------------------------------------------------------------------------------


def test_writes_documents_only_and_never_touches_the_sources_table_at_all():
    """REGRESSION, tightened 19 September 2026. This test used to assert that BOTH `sources` and
    `documents` were written. Under Option B `sources.rule_effective_date` is never added to
    production, so the `UPDATE sources` was removed and the before-value read moved onto
    `documents` too -- which means the assertion had to invert, not merely survive.

    It now proves three things at once: the `documents` write is present, the `sources` write is
    gone, and NO statement of any kind touches `sources`, which is the stronger claim. The read
    mattered as much as the write here: removing only the UPDATE would have left a
    `SELECT rule_effective_date FROM sources` that fails against production with UndefinedColumn
    just as surely.
    """
    conn = FakeConn({FAQ: [date(2026, 9, 15)] * 30})
    sync_annotations(conn, [entry(FAQ, date(2027, 1, 4))])

    # The write is present, targets `documents`, and is the only write.
    assert len(conn.writes) == 1
    assert _written_table(conn.writes[0]) == "documents"
    assert "rule_effective_date = %s" in conn.writes[0]

    # `sources` is untouched by EVERY statement, read or write, not just by the UPDATEs.
    for statement in conn.statements:
        assert not re.search(r"\bsources\b", statement), f"statement touches sources: {statement}"

    for column in NEVER_WRITTEN:
        assert column not in conn.writes[0]


def test_the_never_touched_checks_would_actually_catch_a_violation():
    """The control for the test above. An absence assertion pointed at nothing passes forever, and
    this project has an instrument table full of exactly that. Both predicates are fed the real
    pre-19-September statements and must trip on them.
    """
    old_write = "UPDATE sources SET rule_effective_date = %s WHERE source_url = %s"
    old_read = "SELECT rule_effective_date FROM sources WHERE source_url = %s"
    current_write = "UPDATE documents SET rule_effective_date = %s WHERE source_url = %s"
    current_read = "SELECT DISTINCT rule_effective_date FROM documents WHERE source_url = %s"

    assert _written_table(old_write) == "sources"
    assert _written_table(current_write) == "documents"
    assert re.search(r"\bsources\b", old_write)
    assert re.search(r"\bsources\b", old_read)
    assert not re.search(r"\bsources\b", current_write)
    assert not re.search(r"\bsources\b", current_read)
    # `source_url` must not be mistaken for the `sources` table by the word-boundary check.
    assert not re.search(r"\bsources\b", "WHERE source_url = %s")

    violating = "UPDATE documents SET rule_effective_date = %s, content = %s WHERE source_url = %s"
    assert [c for c in NEVER_WRITTEN if c in violating] == ["content"]
    assert [c for c in NEVER_WRITTEN if c in current_write] == []


# ---------------------------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------------------------


def test_dry_run_issues_no_write_statements_but_still_reports_the_change():
    conn = FakeConn({FAQ: [date(2026, 9, 15)] * 30})
    report = sync_annotations(conn, [entry(FAQ, date(2027, 1, 4))], dry_run=True)

    assert conn.writes == []
    assert len(report.changed) == 1
    assert report.changed[0].before == date(2026, 9, 15)
    assert report.changed[0].after == date(2027, 1, 4)
    assert report.changed[0].documents_rows == 30


def test_no_write_at_all_when_every_chunk_already_holds_the_desired_value():
    """Idempotence, and the state production is in right now: running the tool today must be a
    no-op rather than 53 pointless row rewrites.
    """
    conn = FakeConn({FAQ: [date(2026, 9, 15)] * 30, QUICK: [date(2026, 9, 15)] * 23})
    report = sync_annotations(
        conn, [entry(FAQ, date(2026, 9, 15)), entry(QUICK, date(2026, 9, 15))]
    )

    assert conn.writes == []
    assert report.changed == []
    assert report.to_dict()["unchanged"] == 2


def test_deleting_the_annotation_from_the_manifest_writes_null():
    """The behaviour that is impossible without a manifest-authoritative design: a value read from
    the database and written back re-writes itself forever, so no curator edit can ever remove it.
    Removal is a live option for the two fixed_admission sources, so it has to work.
    """
    conn = FakeConn({FAQ: [date(2026, 9, 15)] * 30})
    report = sync_annotations(conn, [entry(FAQ, None)])

    assert len(conn.writes) == 1
    # Find the write by shape rather than by log position: an added read would silently shift an
    # index and make this assert about the wrong statement.
    update_params = [p for s, p in conn.log if _written_table(s) == "documents"]
    assert len(update_params) == 1
    assert update_params[0][0] is None, "NULL must be the bound value, not a skipped write"
    assert report.changed[0].after is None
    assert conn.docs[FAQ] == [None] * 30


def test_a_manifest_url_with_no_chunks_is_skipped_and_never_inserted():
    """This tool annotates an existing corpus. It is not an ingest, and must never create rows for
    a source that was never crawled.
    """
    conn = FakeConn({})
    report = sync_annotations(conn, [entry("https://example.gov/never-ingested", date(2026, 1, 1))])

    assert conn.writes == []
    assert len(report.missing) == 1
    assert report.to_dict()["not_in_corpus"] == 1


def test_chunks_disagreeing_about_the_date_are_flagged_and_resynced():
    """Only observable since the read moved to `documents`. While the before-value came from
    `sources` there was one row and one value, so a source whose chunks had been left split by a
    partial write looked uniform. The tool must notice, not flatten silently.
    """
    conn = FakeConn({FAQ: [date(2026, 9, 15)] * 20 + [None] * 10})
    report = sync_annotations(conn, [entry(FAQ, date(2026, 9, 15))])

    assert len(report.changed) == 1
    assert report.changed[0].before_is_mixed is True
    assert report.changed[0].before is None, "no single value can honestly be called 'before'"
    assert conn.docs[FAQ] == [date(2026, 9, 15)] * 30


def test_one_transaction_per_source_so_each_is_durable_before_the_next_begins():
    conn = FakeConn({FAQ: [None] * 30, QUICK: [None] * 23})
    sync_annotations(conn, [entry(FAQ, date(2026, 9, 15)), entry(QUICK, date(2026, 9, 15))])

    assert conn.transactions == 2
    assert conn.log[0] == ("BEGIN", None)
    assert conn.log[-1] == ("COMMIT", None)


def test_sources_are_processed_independently_so_one_no_op_does_not_stop_the_next():
    conn = FakeConn({FAQ: [date(2026, 9, 15)] * 30, QUICK: [None] * 23})
    report = sync_annotations(
        conn, [entry(FAQ, date(2026, 9, 15)), entry(QUICK, date(2026, 9, 15))]
    )

    assert len(report.changed) == 1
    assert report.changed[0].source_url == QUICK
    assert len(conn.writes) == 1


def test_documents_rows_reports_how_many_chunks_were_actually_rewritten():
    conn = FakeConn({FAQ: [None] * 53})
    report = sync_annotations(conn, [entry(FAQ, date(2026, 9, 15))])

    assert report.changed[0].documents_rows == 53
    assert report.to_dict()["documents_rows_updated"] == 53


# ---------------------------------------------------------------------------------------------
# manifest_annotation, and the real manifest
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry_dict,expected",
    [
        ({"url": FAQ}, None),
        ({"url": FAQ, "annotations": None}, None),
        ({"url": FAQ, "annotations": {}}, None),
        ({"url": FAQ, "annotations": {"federal_register": "https://example"}}, None),
        (
            {"url": FAQ, "annotations": {"rule_effective_date": date(2026, 9, 15)}},
            date(2026, 9, 15),
        ),
        ({"url": FAQ, "annotations": {"rule_effective_date": "2026-09-15"}}, date(2026, 9, 15)),
    ],
    ids=["no-block", "null-block", "empty", "other-keys-only", "date-object", "iso-string"],
)
def test_manifest_annotation_reads_the_curator_block(entry_dict, expected):
    """None must mean "absent", and absent must be indistinguishable from "explicitly removed", so
    that deleting a key from the manifest reaches the database as NULL.
    """
    assert manifest_annotation(entry_dict, "rule_effective_date") == expected


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "data" / "sources" / "sources.yaml").is_file():
            return candidate
    raise AssertionError("could not locate data/sources/sources.yaml above this test file")


def test_the_real_manifest_parses_and_every_annotation_is_readable():
    """Reads the tracked manifest rather than a fixture, because a YAML typo in the curator block is
    exactly the failure this catches and a fixture cannot. Deliberately asserts NOTHING about which
    sources carry annotations or what the dates are: those are the curator's to change, and a test
    that pinned them would fire on every legitimate edit.
    """
    manifest = yaml.safe_load(
        (_repo_root() / "data" / "sources" / "sources.yaml").read_text(encoding="utf-8")
    )["sources"]

    assert manifest, "the manifest is empty"
    for source in manifest:
        assert "url" in source and "topic" in source
        annotations = source.get("annotations")
        assert annotations is None or isinstance(annotations, dict)
        value = manifest_annotation(source, "rule_effective_date")
        assert value is None or isinstance(value, date)
