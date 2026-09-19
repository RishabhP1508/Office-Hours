"""Tests for app/sync_annotations.py -- the tool that pushes curator annotations from
data/sources/sources.yaml into `sources` and `documents`.

NO DATABASE REQUIRED. Every test here drives the real `sync_annotations` through a fake connection
that records each statement and transaction boundary, so they pass on any machine including
Windows, where the DB-backed tests in this suite cannot run at all (psycopg refuses async mode on
the ProactorEventLoop, and there is usually no local Postgres). With test_check_schema.py these are
the only tests in this project a developer on Windows can execute locally. Worth knowing before
pushing a branch to ask CI something these answer in under a second.

WHAT A FAKE CONNECTION CAN AND CANNOT ESTABLISH. It is strong evidence about WHICH statements are
issued, in what order, inside which transaction, against which columns -- which is exactly what
matters for a tool that writes to production and whose blast radius is documented as a fixed list of
columns. It is no evidence at all about how Postgres responds: `cur.rowcount` semantics and the
real commit boundary of `conn.transaction()` are stubbed here, not observed. Those two were measured
for app/backfill_source_bodies.py by its real run against production on 19 September; for this tool
they remain unobserved, and a passing run of this file does not change that.

PROMOTED FROM A THROWAWAY HARNESS, 19 September 2026. These cases ran as a scratch script while
sync_annotations.py was being written, caught a real defect (a `_SYNCED_ANNOTATIONS` constant that
nothing read while the real column list lived in the SQL), and then sat in a session scratchpad
where nothing could run them again.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from app.sync_annotations import manifest_annotation, sync_annotations

FAQ = "https://studyinthestates.dhs.gov/final-rule-faq"
QUICK = "https://studyinthestates.dhs.gov/final-rule-quick"

# Columns app/sync_annotations.py's docstring promises are NEVER written. Any of these appearing in
# an INSERT/UPDATE/DELETE is a failure of the safety claim, not a style problem. `documents.content`
# and `embedding` are here because writing either would mean re-indexing, which is the entire thing
# this tool exists to avoid.
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


class FakeCursor:
    def __init__(self, state, log):
        self.state, self.log, self.rowcount, self._row = state, log, 0, None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.log.append((flat, params))
        if flat.startswith("SELECT rule_effective_date FROM sources"):
            url = params[0]
            self._row = (self.state["sources"][url],) if url in self.state["sources"] else None
        elif flat.startswith("SELECT count(*) FROM documents"):
            self._row = (self.state["docs"].get(params[0], 0),)
        elif flat.startswith("UPDATE sources"):
            self.state["sources"][params[1]] = params[0]
            self.rowcount = 1
        elif flat.startswith("UPDATE documents"):
            self.rowcount = self.state["docs"].get(params[1], 0)
        else:  # pragma: no cover - a new statement shape must fail loudly, never pass silently
            raise AssertionError(f"unexpected SQL: {flat}")

    def fetchone(self):
        return self._row


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
    """Records every statement and transaction boundary `sync_annotations` issues."""

    def __init__(self, sources: dict[str, date | None], docs: dict[str, int] | None = None):
        self.state = {"sources": dict(sources), "docs": docs or {}}
        self.log: list[tuple[str, object]] = []

    def transaction(self):
        return FakeTxn(self.log)

    def cursor(self):
        return FakeCursor(self.state, self.log)

    @property
    def writes(self) -> list[str]:
        return [s for s, _ in self.log if s.startswith(("UPDATE", "INSERT", "DELETE"))]

    @property
    def transactions(self) -> int:
        return sum(1 for s, _ in self.log if s == "BEGIN")


def entry(url: str, rule_effective_date: date | None = None) -> dict:
    ann = {"rule_effective_date": rule_effective_date} if rule_effective_date else {}
    return {"url": url, "topic": "fixed_admission", "annotations": ann}


# ---------------------------------------------------------------------------------------------
# THE REGRESSION. This is why this file exists rather than only the scenarios below.
# ---------------------------------------------------------------------------------------------


def test_writes_rule_effective_date_and_never_touches_any_other_column():
    """REGRESSION. The tool's docstring makes an exhaustive blast-radius claim: it writes
    `sources.rule_effective_date` and `documents.rule_effective_date`, and NOTHING else. That claim
    is the reason it is safe to run against production by hand, so it is asserted here rather than
    trusted.

    An earlier version carried a `_SYNCED_ANNOTATIONS` tuple that read like the authoritative column
    list and was in fact read by nothing, while the real list lived in the two UPDATE statements. A
    reader could have added a name to it and believed something would happen. The constant was
    removed; this test is what makes the real list checkable.
    """
    conn = FakeConn({FAQ: date(2026, 9, 15)}, {FAQ: 30})
    sync_annotations(conn, [entry(FAQ, date(2027, 1, 4))])

    assert len(conn.writes) == 2
    for statement in conn.writes:
        assert "rule_effective_date = %s" in statement
        for column in NEVER_WRITTEN:
            assert column not in statement, f"{column!r} written by: {statement}"


def test_the_never_written_check_would_actually_catch_a_violation():
    """The control for the test above. A forbidden-column assertion that cannot fail proves
    nothing, and this project has an instrument table full of checks that passed because they were
    pointed at nothing. Feed the same predicate a statement that DOES touch a forbidden column and
    confirm it trips.
    """
    violating = (
        "UPDATE sources SET rule_effective_date = %s, fetched_at = now() WHERE source_url = %s"
    )
    tripped = [c for c in NEVER_WRITTEN if c in violating]
    assert tripped == ["fetched_at"]

    innocent = "UPDATE sources SET rule_effective_date = %s WHERE source_url = %s"
    assert [c for c in NEVER_WRITTEN if c in innocent] == []


def test_both_tables_are_written_not_just_sources():
    """`documents.rule_effective_date` is what retrieval actually reads (app/db.py selects
    `d.rule_effective_date`); `sources` is the per-source mirror. A tool that wrote only `sources`
    would look like it worked and change nothing any guardrail can see -- which is precisely why
    this is not built on app/backfill_source_bodies.py, whose single UPDATE touches `sources` alone.
    """
    conn = FakeConn({FAQ: None}, {FAQ: 30})
    sync_annotations(conn, [entry(FAQ, date(2026, 9, 15))])

    assert [s.split()[1] for s in conn.writes] == ["sources", "documents"]


# ---------------------------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------------------------


def test_dry_run_issues_no_write_statements_but_still_reports_the_change():
    conn = FakeConn({FAQ: date(2026, 9, 15)}, {FAQ: 30})
    report = sync_annotations(conn, [entry(FAQ, date(2027, 1, 4))], dry_run=True)

    assert conn.writes == []
    assert len(report.changed) == 1
    assert report.changed[0].before == date(2026, 9, 15)
    assert report.changed[0].after == date(2027, 1, 4)
    assert report.changed[0].documents_rows == 30


def test_no_write_at_all_when_the_stored_value_already_matches():
    """Idempotence, and the state production is in right now: running the tool today must be a
    no-op rather than 53 pointless row rewrites.
    """
    conn = FakeConn({FAQ: date(2026, 9, 15), QUICK: date(2026, 9, 15)}, {FAQ: 30, QUICK: 23})
    report = sync_annotations(
        conn, [entry(FAQ, date(2026, 9, 15)), entry(QUICK, date(2026, 9, 15))]
    )

    assert conn.writes == []
    assert report.changed == []
    assert report.to_dict()["unchanged"] == 2


def test_deleting_the_annotation_from_the_manifest_writes_null():
    """The behaviour that is impossible without a manifest-authoritative design. When the value is
    read back out of the database and written to the database, a stored value re-writes itself
    forever and no curator edit can ever remove it. Removal is one of the live options for the two
    fixed_admission sources, so it has to work.
    """
    conn = FakeConn({FAQ: date(2026, 9, 15)}, {FAQ: 30})
    report = sync_annotations(conn, [entry(FAQ, None)])

    assert len(conn.writes) == 2
    assert all(
        params[0] is None for statement, params in conn.log if statement.startswith("UPDATE")
    )
    assert report.changed[0].after is None


def test_a_manifest_url_with_no_sources_row_is_skipped_and_never_inserted():
    """This tool syncs annotations onto an existing corpus. It is not an ingest, and must never
    create a row for a source that was never crawled.
    """
    conn = FakeConn({}, {})
    report = sync_annotations(conn, [entry("https://example.gov/never-ingested", date(2026, 1, 1))])

    assert conn.writes == []
    assert len(report.missing) == 1
    assert report.to_dict()["missing_sources_row"] == 1


def test_one_transaction_per_source_so_each_is_durable_before_the_next_begins():
    conn = FakeConn({FAQ: None, QUICK: None}, {FAQ: 30, QUICK: 23})
    sync_annotations(conn, [entry(FAQ, date(2026, 9, 15)), entry(QUICK, date(2026, 9, 15))])

    assert conn.transactions == 2
    assert conn.log[0] == ("BEGIN", None)
    assert conn.log[-1] == ("COMMIT", None)


def test_sources_are_processed_independently_so_one_no_op_does_not_stop_the_next():
    conn = FakeConn({FAQ: date(2026, 9, 15), QUICK: None}, {FAQ: 30, QUICK: 23})
    report = sync_annotations(
        conn, [entry(FAQ, date(2026, 9, 15)), entry(QUICK, date(2026, 9, 15))]
    )

    assert len(report.changed) == 1
    assert report.changed[0].source_url == QUICK
    assert len(conn.writes) == 2


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
        # Must not raise, and must yield a date or None, for every entry including unannotated ones.
        value = manifest_annotation(source, "rule_effective_date")
        assert value is None or isinstance(value, date)
