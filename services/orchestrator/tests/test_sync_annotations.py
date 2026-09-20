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

19 September 2026 (docs/adr/0023-curator-rule-status.md): `sync_annotations` now syncs FOUR
columns together -- `rule_effective_date`, `rule_status`, `rule_status_source`,
`rule_status_source_evidences_status` -- in one SELECT and one UPDATE, and validates the desired
tuple (`app/rule_status.py::validate_rule_status`) before touching the database at all. Every
`FakeConn.docs` row is now a 4-tuple `(rule_effective_date, rule_status, rule_status_source,
rule_status_source_evidences_status)`, not a bare date, and every manifest `entry()` that carries a
`rule_effective_date` now also needs a `rule_status` (and every `rule_status_source` now also needs
a `rule_status_source_evidences_status`) or the tool refuses to run -- see `entry()`'s own docstring
below.
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
# tool exists to avoid. `rule_status`/`rule_status_source` are deliberately NOT in this list: they
# are two of the three columns this tool exists to write, since 19 September 2026.
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


def _never_written_violations(statement: str) -> list[str]:
    """Which of NEVER_WRITTEN's column names appear in `statement`, as whole words -- NOT a plain
    substring test. `\\b` (word boundary) treats `_` as a word character the same way Python
    identifiers do, so `\\bstatus\\b` does not falsely match the `status` inside `rule_status` or
    `rule_status_source` (there is no boundary between `_` and `s`), while it still matches a bare
    `sources.status` write. A plain `column in statement` substring check would have flagged this
    tool's own intentional `rule_status`/`rule_status_source` writes as though they were the
    `sources.status` column this list actually means.
    """
    return [c for c in NEVER_WRITTEN if re.search(rf"\b{re.escape(c)}\b", statement)]


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
        if flat.startswith(
            "SELECT DISTINCT rule_effective_date, rule_status, rule_status_source, "
            "rule_status_source_evidences_status FROM documents"
        ):
            # dict.fromkeys preserves order while de-duplicating, like DISTINCT on the four
            # columns together -- each stored row is already the 4-tuple shape this fake keeps in
            # `self.docs`.
            self._rows = list(dict.fromkeys(self.docs.get(params[0], [])))
        elif flat.startswith("SELECT count(*) FROM documents"):
            self._row = (len(self.docs.get(params[0], [])),)
        elif flat.startswith("UPDATE documents"):
            (
                rule_effective_date,
                rule_status,
                rule_status_source,
                rule_status_source_evidences_status,
                url,
            ) = params
            chunks = self.docs.get(url, [])
            row = (
                rule_effective_date,
                rule_status,
                rule_status_source,
                rule_status_source_evidences_status,
            )
            self.docs[url] = [row] * len(chunks)
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

    `docs` maps a source_url to its PER-CHUNK `(rule_effective_date, rule_status,
    rule_status_source, rule_status_source_evidences_status)` 4-tuples, which is how the real table
    is shaped: one row per chunk. A source absent from this mapping has no chunks at all.
    """

    def __init__(
        self,
        docs: dict[str, list[tuple[date | None, str | None, str | None, bool | None]]],
    ):
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


def entry(
    url: str,
    rule_effective_date: date | None = None,
    rule_status: str | None = None,
    rule_status_source: str | None = None,
    rule_status_source_evidences_status: bool | None = None,
) -> dict:
    """A manifest entry carrying zero or more of the four synced annotations.

    NOTE: since 19 September 2026 (docs/adr/0023-curator-rule-status.md), a `rule_effective_date`
    with no `rule_status` is a HARD ERROR (`app/rule_status.py::validate_rule_status`), raised the
    moment `sync_annotations` reads this entry -- every call site below that sets
    `rule_effective_date` also sets a `rule_status` that is valid for it (usually `"in_force"`,
    since every real date this file uses is safely in the past relative to any date this suite
    could plausibly run on). Likewise a `rule_status_source` with no
    `rule_status_source_evidences_status` is a HARD ERROR -- every call site below that sets
    `rule_status_source` also sets this.
    """
    ann: dict = {}
    if rule_effective_date is not None:
        ann["rule_effective_date"] = rule_effective_date
    if rule_status is not None:
        ann["rule_status"] = rule_status
    if rule_status_source is not None:
        ann["rule_status_source"] = rule_status_source
    if rule_status_source_evidences_status is not None:
        ann["rule_status_source_evidences_status"] = rule_status_source_evidences_status
    return {"url": url, "topic": "fixed_admission", "annotations": ann}


# ---------------------------------------------------------------------------------------------
# THE REGRESSION. This is why this file exists rather than only the scenarios below.
# ---------------------------------------------------------------------------------------------


def test_writes_documents_only_and_never_touches_the_sources_table_at_all():
    """REGRESSION, tightened 19 September 2026. This test used to assert that BOTH `sources` and
    `documents` were written. Under Option B `sources.rule_effective_date` (and, from the start,
    `rule_status`/`rule_status_source`) is never added to production, so the `UPDATE sources` was
    removed and the before-value read moved onto `documents` too -- which means the assertion had
    to invert, not merely survive.

    It now proves three things at once: the `documents` write is present (and now carries all
    four synced columns), the `sources` write is gone, and NO statement of any kind touches
    `sources`, which is the stronger claim. The read mattered as much as the write here: removing
    only the UPDATE would have left a `SELECT ... FROM sources` that fails against production with
    UndefinedColumn just as surely.
    """
    conn = FakeConn({FAQ: [(date(2026, 9, 15), "in_force", None, None)] * 30})
    sync_annotations(conn, [entry(FAQ, date(2027, 1, 4), rule_status="scheduled")])

    # The write is present, targets `documents`, and is the only write.
    assert len(conn.writes) == 1
    assert _written_table(conn.writes[0]) == "documents"
    assert "rule_effective_date = %s" in conn.writes[0]
    assert "rule_status = %s" in conn.writes[0]
    assert "rule_status_source = %s" in conn.writes[0]
    assert "rule_status_source_evidences_status = %s" in conn.writes[0]

    # `sources` is untouched by EVERY statement, read or write, not just by the UPDATEs.
    for statement in conn.statements:
        assert not re.search(r"\bsources\b", statement), f"statement touches sources: {statement}"

    assert _never_written_violations(conn.writes[0]) == []


def test_the_never_touched_checks_would_actually_catch_a_violation():
    """The control for the test above. An absence assertion pointed at nothing passes forever, and
    this project has an instrument table full of exactly that. Both predicates are fed the real
    pre-19-September statements and must trip on them.
    """
    old_write = "UPDATE sources SET rule_effective_date = %s WHERE source_url = %s"
    old_read = "SELECT rule_effective_date FROM sources WHERE source_url = %s"
    current_write = (
        "UPDATE documents SET rule_effective_date = %s, rule_status = %s, "
        "rule_status_source = %s, rule_status_source_evidences_status = %s WHERE source_url = %s"
    )
    current_read = (
        "SELECT DISTINCT rule_effective_date, rule_status, rule_status_source, "
        "rule_status_source_evidences_status FROM documents WHERE source_url = %s"
    )

    assert _written_table(old_write) == "sources"
    assert _written_table(current_write) == "documents"
    assert re.search(r"\bsources\b", old_write)
    assert re.search(r"\bsources\b", old_read)
    assert not re.search(r"\bsources\b", current_write)
    assert not re.search(r"\bsources\b", current_read)
    # `source_url` must not be mistaken for the `sources` table by the word-boundary check.
    assert not re.search(r"\bsources\b", "WHERE source_url = %s")

    violating = "UPDATE documents SET rule_effective_date = %s, content = %s WHERE source_url = %s"
    assert _never_written_violations(violating) == ["content"]
    assert _never_written_violations(current_write) == []


# ---------------------------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------------------------


def test_dry_run_issues_no_write_statements_but_still_reports_the_change():
    conn = FakeConn({FAQ: [(date(2026, 9, 15), "in_force", None, None)] * 30})
    report = sync_annotations(
        conn, [entry(FAQ, date(2027, 1, 4), rule_status="scheduled")], dry_run=True
    )

    assert conn.writes == []
    assert len(report.changed) == 1
    assert report.changed[0].before == (date(2026, 9, 15), "in_force", None, None)
    assert report.changed[0].after == (date(2027, 1, 4), "scheduled", None, None)
    assert report.changed[0].documents_rows == 30


def test_no_write_at_all_when_every_chunk_already_holds_the_desired_value():
    """Idempotence, and the state production is in right now: running the tool today must be a
    no-op rather than 53 pointless row rewrites.
    """
    conn = FakeConn(
        {
            FAQ: [(date(2026, 9, 15), "in_force", None, None)] * 30,
            QUICK: [(date(2026, 9, 15), "in_force", None, None)] * 23,
        }
    )
    report = sync_annotations(
        conn,
        [
            entry(FAQ, date(2026, 9, 15), rule_status="in_force"),
            entry(QUICK, date(2026, 9, 15), rule_status="in_force"),
        ],
    )

    assert conn.writes == []
    assert report.changed == []
    assert report.to_dict()["unchanged"] == 2


def test_deleting_the_annotation_from_the_manifest_writes_null():
    """The behaviour that is impossible without a manifest-authoritative design: a value read from
    the database and written back re-writes itself forever, so no curator edit can ever remove it.
    Removal is a live option for the two fixed_admission sources, so it has to work -- for all
    four columns at once, since deleting the whole `annotations` block (or just `rule_status`)
    must not leave a stale `rule_effective_date`/`rule_status_source`/
    `rule_status_source_evidences_status` behind.
    """
    conn = FakeConn({FAQ: [(date(2026, 9, 15), "in_force", None, None)] * 30})
    report = sync_annotations(conn, [entry(FAQ)])

    assert len(conn.writes) == 1
    # Find the write by shape rather than by log position: an added read would silently shift an
    # index and make this assert about the wrong statement.
    update_params = [p for s, p in conn.log if _written_table(s) == "documents"]
    assert len(update_params) == 1
    assert update_params[0] == (
        None,
        None,
        None,
        None,
        FAQ,
    ), "NULL must be bound for all four, not skipped"
    assert report.changed[0].after == (None, None, None, None)
    assert conn.docs[FAQ] == [(None, None, None, None)] * 30


def test_a_manifest_url_with_no_chunks_is_skipped_and_never_inserted():
    """This tool annotates an existing corpus. It is not an ingest, and must never create rows for
    a source that was never crawled.
    """
    conn = FakeConn({})
    report = sync_annotations(
        conn,
        [entry("https://example.gov/never-ingested", date(2026, 1, 1), rule_status="in_force")],
    )

    assert conn.writes == []
    assert len(report.missing) == 1
    assert report.to_dict()["not_in_corpus"] == 1


def test_chunks_disagreeing_about_the_date_are_flagged_and_resynced():
    """Only observable since the read moved to `documents`. While the before-value came from
    `sources` there was one row and one value, so a source whose chunks had been left split by a
    partial write looked uniform. The tool must notice, not flatten silently.
    """
    conn = FakeConn(
        {FAQ: [(date(2026, 9, 15), "in_force", None, None)] * 20 + [(None, None, None, None)] * 10}
    )
    report = sync_annotations(conn, [entry(FAQ, date(2026, 9, 15), rule_status="in_force")])

    assert len(report.changed) == 1
    assert report.changed[0].before_is_mixed is True
    assert report.changed[0].before == (
        None,
        None,
        None,
        None,
    ), "no single value can honestly be called 'before'"
    assert conn.docs[FAQ] == [(date(2026, 9, 15), "in_force", None, None)] * 30


def test_one_transaction_per_source_so_each_is_durable_before_the_next_begins():
    conn = FakeConn({FAQ: [(None, None, None, None)] * 30, QUICK: [(None, None, None, None)] * 23})
    sync_annotations(
        conn,
        [
            entry(FAQ, date(2026, 9, 15), rule_status="in_force"),
            entry(QUICK, date(2026, 9, 15), rule_status="in_force"),
        ],
    )

    assert conn.transactions == 2
    assert conn.log[0] == ("BEGIN", None)
    assert conn.log[-1] == ("COMMIT", None)


def test_sources_are_processed_independently_so_one_no_op_does_not_stop_the_next():
    conn = FakeConn(
        {
            FAQ: [(date(2026, 9, 15), "in_force", None, None)] * 30,
            QUICK: [(None, None, None, None)] * 23,
        }
    )
    report = sync_annotations(
        conn,
        [
            entry(FAQ, date(2026, 9, 15), rule_status="in_force"),
            entry(QUICK, date(2026, 9, 15), rule_status="in_force"),
        ],
    )

    assert len(report.changed) == 1
    assert report.changed[0].source_url == QUICK
    assert len(conn.writes) == 1


def test_documents_rows_reports_how_many_chunks_were_actually_rewritten():
    conn = FakeConn({FAQ: [(None, None, None, None)] * 53})
    report = sync_annotations(conn, [entry(FAQ, date(2026, 9, 15), rule_status="in_force")])

    assert report.changed[0].documents_rows == 53
    assert report.to_dict()["documents_rows_updated"] == 53


# ---------------------------------------------------------------------------------------------
# rule_status / rule_status_source / rule_status_source_evidences_status
# (docs/adr/0023-curator-rule-status.md)
# ---------------------------------------------------------------------------------------------


def test_enjoined_status_and_its_source_sync_together_in_one_write():
    """The live case: a curator flips a source from `in_force` to `enjoined` and supplies the
    required `rule_status_source` (and its required `rule_status_source_evidences_status`) in the
    same manifest edit. All land in the SAME UPDATE as `rule_effective_date`, proving the four
    columns move as one annotation, not four.
    """
    conn = FakeConn({FAQ: [(date(2026, 9, 15), "in_force", None, None)] * 30})
    report = sync_annotations(
        conn,
        [
            entry(
                FAQ,
                date(2026, 9, 15),
                rule_status="enjoined",
                rule_status_source="https://www.federalregister.gov/d/2026-14439",
                rule_status_source_evidences_status=False,
            )
        ],
    )

    assert len(conn.writes) == 1
    assert report.changed[0].after == (
        date(2026, 9, 15),
        "enjoined",
        "https://www.federalregister.gov/d/2026-14439",
        False,
    )
    assert (
        conn.docs[FAQ]
        == [(date(2026, 9, 15), "enjoined", "https://www.federalregister.gov/d/2026-14439", False)]
        * 30
    )


def test_raises_on_a_manifest_entry_with_a_date_and_no_status():
    """The hard error this whole change exists to enforce, exercised through the tool a curator
    actually runs: a `rule_effective_date` with no `rule_status` must never reach the database, in
    a sync any more than in an ingest.
    """
    conn = FakeConn({FAQ: [(None, None, None, None)] * 30})
    with pytest.raises(ValueError, match="rule_effective_date is set"):
        sync_annotations(conn, [entry(FAQ, date(2026, 9, 15))])
    assert conn.writes == [], "nothing may be written once validation has failed"


def test_raises_on_enjoined_with_no_rule_status_source():
    """`enjoined` requires a citable source (`app/rule_status.py::validate_rule_status`) -- a
    manifest entry missing it is refused here, at sync time, exactly as it would be at the next
    full ingest.
    """
    conn = FakeConn({FAQ: [(date(2026, 9, 15), "in_force", None, None)] * 30})
    with pytest.raises(ValueError, match="rule_status_source"):
        sync_annotations(conn, [entry(FAQ, date(2026, 9, 15), rule_status="enjoined")])
    assert conn.writes == []


def test_raises_on_a_rule_status_source_with_no_evidences_status():
    """The hard error item 1 of docs/adr/0023-curator-rule-status.md exists to enforce: a
    `rule_status_source` with no `rule_status_source_evidences_status` must never reach the
    database, in a sync any more than in an ingest -- it is never inferred from the URL.
    """
    conn = FakeConn({FAQ: [(date(2026, 9, 15), "in_force", None, None)] * 30})
    with pytest.raises(ValueError, match="rule_status_source_evidences_status"):
        sync_annotations(
            conn,
            [
                entry(
                    FAQ,
                    date(2026, 9, 15),
                    rule_status="enjoined",
                    rule_status_source="https://www.federalregister.gov/d/2026-14439",
                )
            ],
        )
    assert conn.writes == []


def test_not_in_force_with_no_date_at_all_is_a_valid_sync():
    """`not_in_force` (like `enjoined`) may carry no `rule_effective_date` at all -- a vacated or
    withdrawn rule's own effective date is not always worth stating. This must sync cleanly, not
    be mistaken for the "date with no status" hard error (there is no date here at all).
    """
    conn = FakeConn({FAQ: [(date(2026, 9, 15), "in_force", None, None)] * 12})
    report = sync_annotations(
        conn,
        [
            entry(
                FAQ,
                rule_status="not_in_force",
                rule_status_source="https://www.federalregister.gov/withdrawal",
                rule_status_source_evidences_status=True,
            )
        ],
    )

    assert len(conn.writes) == 1
    assert report.changed[0].after == (
        None,
        "not_in_force",
        "https://www.federalregister.gov/withdrawal",
        True,
    )


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


def test_the_real_manifest_validates_under_rule_status_rules():
    """The real, tracked manifest must itself satisfy `app/rule_status.py::validate_rule_status`
    for every entry -- this is the check that would have caught a curator committing a
    `rule_effective_date` with no `rule_status`, an `enjoined`/`not_in_force` entry with no
    `rule_status_source`, or a `rule_status_source` with no (or non-boolean)
    `rule_status_source_evidences_status`, directly, rather than waiting for the next ingest or sync
    run to refuse it. Uses `date.today()`, the real clock, deliberately: this is a property the
    manifest must hold on whatever day this test happens to run, the same way the manifest itself is
    read fresh on whatever day a real ingest runs.
    """
    from app.rule_status import validate_rule_status
    from app.sync_annotations import manifest_annotation_bool, manifest_annotation_str

    manifest = yaml.safe_load(
        (_repo_root() / "data" / "sources" / "sources.yaml").read_text(encoding="utf-8")
    )["sources"]

    for source in manifest:
        validate_rule_status(
            source_url=source["url"],
            rule_status=manifest_annotation_str(source, "rule_status"),
            rule_effective_date=manifest_annotation(source, "rule_effective_date"),
            rule_status_source=manifest_annotation_str(source, "rule_status_source"),
            rule_status_source_evidences_status=manifest_annotation_bool(
                source, "rule_status_source_evidences_status"
            ),
            today=date.today(),
        )


# ---------------------------------------------------------------------------------------------
# The manifest-agreement test: two tracked files (data/sources/sources.yaml and
# eval/fixtures/sources/*.md) independently describe the same source_url, and nothing compared
# them until now. Placed here rather than in tests/test_chunking.py because this file already reads
# the tracked manifest for the same reason (test_the_real_manifest_parses_and_every_annotation_is_
# readable, test_the_real_manifest_validates_under_rule_status_rules, immediately above), while
# test_chunking.py's fixture-corpus knowledge is about chunking behavior, not curator annotations.
# ---------------------------------------------------------------------------------------------


def test_fixture_corpus_agrees_with_the_manifest_on_shared_source_urls():
    """The check that would have caught eval/fixtures/sources/fixed_admission-studyinthestates-
    final-rule-faq.md asserting `rule_status: in_force` while data/sources/sources.yaml annotates
    the SAME source_url `enjoined` -- two tracked files, both hand-maintained, independently
    describing the same real-world source, with nothing comparing them before this test existed.

    No database, no langgraph: both files are tracked in git and parse with plain yaml/frontmatter.
    Locates the manifest the same way this file's other real-manifest tests do (`_repo_root()`);
    locates the fixture directory the same way, relative to that same repo root, so both come from
    one consistent notion of "the checkout this test is running in".

    MANDATORY, and the part that makes this a real check rather than a check-shaped no-op: the set
    of shared source_urls must be non-empty, and at least one of them must carry a `rule_status` in
    the manifest. A comparison over zero pairs -- or over pairs where neither side ever states the
    field this test exists to compare -- passes forever and proves nothing, which is the exact
    defect shape (an instrument that could never have failed) this project keeps recording.
    """
    from app.ingest import (
        _parse_iso_date,
        manifest_annotation_bool,
        manifest_annotation_str,
        parse_frontmatter,
    )

    repo_root = _repo_root()
    manifest = yaml.safe_load(
        (repo_root / "data" / "sources" / "sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    manifest_by_url = {source["url"]: source for source in manifest}

    fixtures_dir = repo_root / "eval" / "fixtures" / "sources"
    if not fixtures_dir.is_dir():
        pytest.skip(f"{fixtures_dir} does not exist")
    fixture_paths = sorted(fixtures_dir.glob("*.md"))
    if not fixture_paths:
        pytest.skip(f"no fixture files found in {fixtures_dir}")

    shared_urls: list[str] = []
    a_shared_url_carries_rule_status = False
    disagreements: list[str] = []

    for path in fixture_paths:
        frontmatter, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        source_url = frontmatter.get("source_url")
        manifest_entry = manifest_by_url.get(source_url) if source_url else None
        if manifest_entry is None:
            continue
        shared_urls.append(source_url)

        manifest_values = (
            manifest_annotation(manifest_entry, "rule_effective_date"),
            manifest_annotation_str(manifest_entry, "rule_status"),
            manifest_annotation_str(manifest_entry, "rule_status_source"),
            manifest_annotation_bool(manifest_entry, "rule_status_source_evidences_status"),
        )
        fixture_values = (
            _parse_iso_date(frontmatter.get("rule_effective_date")),
            frontmatter.get("rule_status"),
            frontmatter.get("rule_status_source"),
            frontmatter.get("rule_status_source_evidences_status"),
        )

        if manifest_values[1] is not None:
            a_shared_url_carries_rule_status = True

        if manifest_values != fixture_values:
            disagreements.append(
                f"{source_url}: manifest annotations {manifest_values!r} != "
                f"fixture frontmatter {fixture_values!r}"
            )

    assert shared_urls, (
        "no source_url appears in both data/sources/sources.yaml and eval/fixtures/sources/*.md -- "
        "a comparison over zero pairs passes forever and proves nothing"
    )
    assert a_shared_url_carries_rule_status, (
        "none of the shared source_urls carries a rule_status in the manifest -- this comparison "
        "is not exercising the field it exists for"
    )
    assert disagreements == [], "\n".join(disagreements)
