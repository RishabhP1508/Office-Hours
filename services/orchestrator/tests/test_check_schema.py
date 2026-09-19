"""Tests for app/check_schema.py -- the schema drift check
(.github/workflows/drift-checks.yml).

NO DATABASE REQUIRED. Every test in this file runs against in-memory structures or a fake
connection, so it passes on any machine including Windows, where the DB-backed tests in this suite
cannot run at all (psycopg refuses async mode on the ProactorEventLoop, and there is usually no
local Postgres). Together with test_sync_annotations.py these are the only tests here that a
developer on Windows can actually execute locally, which is worth knowing before reaching for CI to
answer a question these can answer in a second.

That is not an accident of how they were written. `compare` and `render` are pure by construction,
and `read_schema` is one SELECT, so the only thing a real database would add is proof that
`information_schema` returns what Postgres documents it returns. The first CI run covers that.

PROMOTED FROM A THROWAWAY HARNESS, 19 September 2026. These cases ran as a scratch script while
check_schema.py was being written and caught a real defect (see
`test_headline_never_says_ok_when_the_exit_code_is_nonzero`), then lived in a session scratchpad
where nothing could ever run them again. Promoting them is the same fix as putting the curator
annotations in a tracked file: the artifact that establishes correctness has to live somewhere the
project can find.
"""

from __future__ import annotations

import pytest

from app.check_schema import Column, Drift, compare, read_schema, render


def col(name: str, dt: str = "text", *, nullable: bool = True, default: bool = False) -> Column:
    return Column(name=name, data_type=dt, nullable=nullable, has_default=default)


def schema(**tables: list[Column]) -> dict[str, dict[str, Column]]:
    return {table: {c.name: c for c in cols} for table, cols in tables.items()}


# A reduction of what infra/sql/init.sql declares, to the columns these cases turn on.
REFERENCE = schema(
    sources=[
        col("source_url"),
        col("last_verified_at", "timestamp with time zone", nullable=False),
        col("last_indexed_body"),
        col("rule_effective_date", "date"),
    ],
    documents=[
        col("id", "bigint", nullable=False, default=True),
        col("content", "text", nullable=False),
        col("rule_effective_date", "date"),
        col("content_tsv", "tsvector"),
    ],
    usage_totals=[col("total_queries", "integer", nullable=False, default=True)],
)


def _without(table: str, column: str) -> dict[str, dict[str, Column]]:
    """REFERENCE minus one column, as a target database would look missing it."""
    return {
        t: {k: v for k, v in cols.items() if not (t == table and k == column)}
        for t, cols in REFERENCE.items()
    }


# ---------------------------------------------------------------------------------------------
# THE REGRESSION. This is why this file exists rather than only the scenarios below.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target,why",
    [
        (_without("sources", "last_indexed_body"), "a missing column"),
        ({k: v for k, v in REFERENCE.items() if k != "usage_totals"}, "a missing table"),
        (
            {
                **REFERENCE,
                "sources": {
                    **REFERENCE["sources"],
                    "mandatory": col("mandatory", nullable=False, default=False),
                },
            },
            "an extra NOT NULL column with no default",
        ),
    ],
    ids=["missing-column", "missing-table", "fatal-extra-column"],
)
def test_headline_never_says_ok_when_the_exit_code_is_nonzero(target, why):
    """REGRESSION, 19 September 2026. `render` derived its headline from the missing-object test
    alone, so a drift whose ONLY fault was a fatal extra column printed `schema ok` as its first
    line and then exited 1. A green headline on a red outcome, produced by the check built to catch
    exactly that shape.

    The fix was to derive the headline from `drift.ok`, the same property that decides the exit
    code. This asserts the invariant rather than the fix: for every way a drift can be fatal, the
    first line must not claim ok. The `fatal-extra-column` case is the one that actually regressed;
    the other two are there so the rule is tested as a class rather than at the one point that
    failed.
    """
    drift = compare(REFERENCE, target)
    assert drift.ok is False, f"expected {why} to be fatal"

    headline = render(drift, expected=REFERENCE).splitlines()[0]
    assert not headline.startswith("schema ok"), (
        f"headline claims ok for {why}, but drift.ok is False and the process will exit 1: "
        f"{headline!r}"
    )
    assert "SCHEMA DRIFT" in headline


def test_headline_says_ok_only_when_the_drift_really_is_ok():
    """The other half of the same invariant, so the rule above cannot be satisfied by a render that
    never says ok at all.
    """
    drift = compare(REFERENCE, REFERENCE)
    assert drift.ok is True
    assert render(drift, expected=REFERENCE).splitlines()[0].startswith("schema ok")


# ---------------------------------------------------------------------------------------------
# The comparison rules
# ---------------------------------------------------------------------------------------------


def test_missing_columns_are_fatal_and_named():
    """Production as found on 19 September 2026: frozen pre-ADR-0014, both `sources` additions
    absent. Confirmed against the real information_schema as false/false/true/true.
    """
    target = {
        **REFERENCE,
        "sources": {
            k: v
            for k, v in REFERENCE["sources"].items()
            if k not in {"last_indexed_body", "rule_effective_date"}
        },
    }
    drift = compare(REFERENCE, target)

    assert drift.ok is False
    assert sorted(c for _, c, _ in drift.missing_columns) == [
        "last_indexed_body",
        "rule_effective_date",
    ]
    out = render(drift, expected=REFERENCE)
    assert "sources.last_indexed_body" in out
    assert "sources.rule_effective_date" in out


def test_a_missing_table_is_fatal():
    """app/usage.py records production having been pointed at a database with no `usage_totals`,
    turning a correct, grounded, cited answer into a 502. Same rule, no extra machinery.
    """
    drift = compare(REFERENCE, {k: v for k, v in REFERENCE.items() if k != "usage_totals"})
    assert drift.ok is False
    assert drift.missing_tables == ["usage_totals"]
    assert "MISSING TABLE   usage_totals" in render(drift, expected=REFERENCE)


def test_a_nullable_extra_column_is_reported_but_never_fatal():
    """The developer-machine case after Option B removes `sources.rule_effective_date` from
    init.sql: the column is still present locally. This MUST stay green. A check that failed here
    would be red on every developer machine for a condition that breaks nothing, which is how a
    check gets muted rather than fixed.
    """
    expected = _without("sources", "rule_effective_date")
    drift = compare(expected, REFERENCE)

    assert drift.ok is True
    assert drift.extra_columns == [("sources", "rule_effective_date", False)]
    out = render(drift, expected=expected)
    assert out.splitlines()[0].startswith("schema ok")
    assert "NOT fatal" in out


def test_an_extra_not_null_column_with_no_default_is_fatal():
    """The one way a column nothing references can still break the system: every INSERT that does
    not name it fails.
    """
    target = {
        **REFERENCE,
        "sources": {
            **REFERENCE["sources"],
            "mandatory": col("mandatory", nullable=False, default=False),
        },
    }
    drift = compare(REFERENCE, target)

    assert drift.ok is False
    assert drift.fatal_extra_columns == [("sources", "mandatory", True)]
    assert "BREAK INSERTS" in render(drift, expected=REFERENCE)


def test_an_extra_not_null_column_WITH_a_default_is_not_fatal():
    """The boundary of the rule above. A default means INSERTs that omit it still succeed, so it is
    an ordinary benign extra. Without this, the rule could be "NOT NULL is fatal", which would fire
    on every defaulted column anyone ever adds.
    """
    target = {
        **REFERENCE,
        "sources": {
            **REFERENCE["sources"],
            "counted": col("counted", "integer", nullable=False, default=True),
        },
    }
    drift = compare(REFERENCE, target)

    assert drift.ok is True
    assert drift.extra_columns == [("sources", "counted", False)]


def test_an_exact_match_is_ok_and_reports_no_drift():
    drift = compare(REFERENCE, REFERENCE)
    assert (drift.ok, drift.missing_tables, drift.missing_columns) == (True, [], [])
    assert (drift.extra_tables, drift.extra_columns) == ([], [])


def test_an_extra_table_is_reported_but_not_fatal():
    target = {**REFERENCE, "some_other_apps_table": {"id": col("id")}}
    drift = compare(REFERENCE, target)
    assert drift.ok is True
    assert drift.extra_tables == ["some_other_apps_table"]


def test_to_dict_marks_which_extra_columns_are_fatal():
    """The JSON form is what a human reads out of a CI artifact, so it has to distinguish the two
    kinds of extra rather than listing them together.
    """
    target = {
        **REFERENCE,
        "sources": {
            **REFERENCE["sources"],
            "benign": col("benign"),
            "mandatory": col("mandatory", nullable=False, default=False),
        },
    }
    payload = compare(REFERENCE, target).to_dict()
    assert payload["ok"] is False
    assert "sources.benign" in payload["extra_columns"]
    assert "sources.mandatory [NOT NULL, no default -- FATAL]" in payload["extra_columns"]


# ---------------------------------------------------------------------------------------------
# read_schema, the one part that talks to a database, driven through a fake connection
# ---------------------------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj


def test_read_schema_shapes_rows_and_reads_nullability_and_defaults():
    conn = _FakeConn(
        [
            ("sources", "source_url", "text", "NO", None),
            ("sources", "change_count", "integer", "NO", "0"),
            ("sources", "last_indexed_body", "text", "YES", None),
        ]
    )
    result = read_schema(conn)

    assert set(result) == {"sources"}
    assert result["sources"]["source_url"].nullable is False
    assert result["sources"]["source_url"].has_default is False
    assert result["sources"]["change_count"].has_default is True
    assert result["sources"]["last_indexed_body"].nullable is True
    # NOT NULL with a default must not be flagged as insert-breaking.
    assert result["sources"]["change_count"].breaks_inserts is False
    assert result["sources"]["source_url"].breaks_inserts is True


def test_read_schema_is_read_only_and_scoped_to_the_public_schema():
    """One SELECT, no writes. The check is pointed at production, so "it only ever reads" is a
    property worth asserting rather than assuming.
    """
    conn = _FakeConn([])
    read_schema(conn)

    assert len(conn.cursor_obj.executed) == 1
    statement = conn.cursor_obj.executed[0]
    assert statement.startswith("SELECT")
    assert "table_schema = 'public'" in statement
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"):
        assert forbidden not in statement.upper()


def test_an_empty_drift_is_ok():
    """`Drift()` with nothing in it is the green case; guards against `ok` being written as a
    truthiness test over the lists that inverts when they are all empty.
    """
    assert Drift().ok is True
