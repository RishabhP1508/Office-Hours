"""Compare a target database's schema against the schema `infra/sql/init.sql` declares, and fail
loudly on anything missing.

THE EXPECTED SCHEMA IS NOT PARSED OUT OF init.sql. It is read from a REFERENCE database that has had
init.sql applied to it, so Postgres does the parsing and this module only ever compares two
`information_schema` queries against each other. See .github/workflows/drift-checks.yml for the full
argument; the short version is that init.sql's net column set is not a simple read (CREATE TABLE,
plus multi-column ADD COLUMN IF NOT EXISTS with comments interleaved, plus a DROP COLUMN inside a
conditional DO block) and a hand-rolled parser would be authoritative about the one thing it
could be quietly wrong about.

WHAT COUNTS AS A FAILURE, and the asymmetry is deliberate:

  MISSING table, or MISSING column  -> FATAL.
      A guaranteed runtime failure with a known severity: code references it, it is not there,
      psycopg raises UndefinedColumn. This is the case that actually happened (production frozen
      pre-ADR-0014; the first re-crawl would have marked all 14 sources fetch_failed without
      fetching a page -- see REPORT.md, instrument table entry 33).

  EXTRA table, or EXTRA column      -> REPORTED, never fatal.
      By construction nothing references it: if code referenced it, it would be declared in
      init.sql, and then it would not be extra. It cannot break anything. After Option B lands,
      `sources.rule_effective_date` is exactly this case on every developer database created before
      that change, and a check that failed on it would be red everywhere for a condition that
      breaks nothing -- which is how people learn to ignore a check.

  EXTRA column that is NOT NULL with no default -> FATAL.
      The one way an unreferenced column can actually hurt: every INSERT that does not name it
      fails. Narrow on purpose, so it catches that and stays quiet about nullable leftovers.

TYPES ARE NOT COMPARED, deliberately, in this first version. `text` vs `character varying`, and
reporting differences between a pinned pg16 reference and whatever the hosted target runs, produce
false positives, and a check that is noisy on its first outing gets muted rather than fixed. Add it
once the name comparison has run clean for a while.

Runnable as:

    python -m app.check_schema --reference-url postgresql://... [--target-url postgresql://...]

`--target-url` defaults to DATABASE_URL. Read-only against both databases: it issues one SELECT
against `information_schema.columns` per connection and writes nothing anywhere.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

import psycopg

from app.config import Settings, get_settings


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    nullable: bool
    has_default: bool

    @property
    def breaks_inserts(self) -> bool:
        """An extra column that every INSERT not naming it would fail on."""
        return not self.nullable and not self.has_default


def read_schema(conn: psycopg.Connection) -> dict[str, dict[str, Column]]:
    """`{table_name: {column_name: Column}}` for the `public` schema. One SELECT, read-only."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """
        )
        schema: dict[str, dict[str, Column]] = {}
        for table, column, data_type, is_nullable, default in cur.fetchall():
            schema.setdefault(table, {})[column] = Column(
                name=column,
                data_type=data_type,
                nullable=(is_nullable == "YES"),
                has_default=default is not None,
            )
    return schema


@dataclass
class Drift:
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[tuple[str, str, str]] = field(default_factory=list)
    extra_tables: list[str] = field(default_factory=list)
    extra_columns: list[tuple[str, str, bool]] = field(default_factory=list)

    @property
    def fatal_extra_columns(self) -> list[tuple[str, str, bool]]:
        return [c for c in self.extra_columns if c[2]]

    @property
    def ok(self) -> bool:
        return not (self.missing_tables or self.missing_columns or self.fatal_extra_columns)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "missing_tables": self.missing_tables,
            "missing_columns": [f"{t}.{c} {d}" for t, c, d in self.missing_columns],
            "extra_tables": self.extra_tables,
            "extra_columns": [
                f"{t}.{c}" + (" [NOT NULL, no default -- FATAL]" if fatal else "")
                for t, c, fatal in self.extra_columns
            ],
        }


def compare(expected: dict[str, dict[str, Column]], actual: dict[str, dict[str, Column]]) -> Drift:
    """Diff `actual` (the target) against `expected` (the reference built from init.sql)."""
    drift = Drift()

    for table in sorted(expected):
        if table not in actual:
            drift.missing_tables.append(table)
            continue
        for name, col in expected[table].items():
            if name not in actual[table]:
                drift.missing_columns.append((table, name, col.data_type))

    for table in sorted(actual):
        if table not in expected:
            drift.extra_tables.append(table)
            continue
        for name, col in actual[table].items():
            if name not in expected[table]:
                drift.extra_columns.append((table, name, col.breaks_inserts))

    return drift


def render(drift: Drift, *, expected: dict[str, dict[str, Column]]) -> str:
    """Two deliberately different message shapes, so a red run never leaves anyone asking which
    check fired. This job's vocabulary is schema; the staleness job's is corpus age.
    """
    table_count = len(expected)
    column_count = sum(len(cols) for cols in expected.values())
    lines: list[str] = []

    # The headline is derived from `drift.ok`, the SAME property that decides the exit code, never
    # from the missing-object check alone. An earlier version keyed it on missing objects only, so a
    # run whose only fault was a fatal extra column printed "schema ok" as its first line and then
    # exited 1 -- a green headline on a red outcome, which is precisely the defect this check exists
    # to catch, reproduced in the check's own output. One source of truth for the verdict.
    if not drift.ok:
        faults = (
            len(drift.missing_tables) + len(drift.missing_columns) + len(drift.fatal_extra_columns)
        )
        lines.append(f"SCHEMA DRIFT: {faults} fault(s) against what infra/sql/init.sql declares")
    else:
        lines.append(
            f"schema ok: {table_count} tables, {column_count} columns present in the target"
        )

    if drift.missing_tables or drift.missing_columns:
        for table in drift.missing_tables:
            lines.append(f"  MISSING TABLE   {table}")
        for table, column, data_type in drift.missing_columns:
            lines.append(f"  MISSING COLUMN  {table}.{column}  {data_type}")
        lines.append(
            "  -> code referencing these fails at runtime with UndefinedTable/UndefinedColumn"
        )

    if drift.fatal_extra_columns:
        lines.append("")
        lines.append("EXTRA COLUMNS THAT BREAK INSERTS (NOT NULL with no default):")
        for table, column, _ in drift.fatal_extra_columns:
            lines.append(f"  {table}.{column}")

    benign_extra = [c for c in drift.extra_columns if not c[2]]
    if benign_extra or drift.extra_tables:
        lines.append("")
        lines.append(
            f"{len(benign_extra) + len(drift.extra_tables)} extra object(s) in the target, "
            f"not declared by init.sql. NOT fatal: nothing references them."
        )
        for table in drift.extra_tables:
            lines.append(f"  extra table   {table}")
        for table, column, _ in benign_extra:
            lines.append(f"  extra column  {table}.{column}  (nullable or defaulted)")

    return "\n".join(lines)


def _mask_dsn(dsn: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)


def _connect(dsn: str, label: str) -> psycopg.Connection:
    if not dsn:
        raise SystemExit(
            f"check_schema: no {label} connection string\n"
            f"\n"
            f"The reference is a database with infra/sql/init.sql applied to it; the\n"
            f"target is the database being checked (defaults to DATABASE_URL).\n"
        )
    try:
        return psycopg.connect(dsn, connect_timeout=10)
    except psycopg.Error as exc:
        raise SystemExit(
            f"check_schema: could not connect to the {label} database\n"
            f"\n"
            f"  dsn   : {_mask_dsn(dsn)}\n"
            f"  error : {type(exc).__name__}: {str(exc).strip()}\n"
        ) from None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.check_schema",
        description=(
            "Compare a target database's schema against a reference database built from "
            "infra/sql/init.sql. Read-only against both."
        ),
    )
    parser.add_argument(
        "--reference-url",
        required=True,
        help="DSN of a database with infra/sql/init.sql already applied",
    )
    parser.add_argument("--target-url", default=None, help="DSN to check; defaults to DATABASE_URL")
    parser.add_argument("--json", action="store_true", help="print the drift report as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings: Settings = get_settings()
    target_dsn = args.target_url or settings.DATABASE_URL

    ref_conn = _connect(args.reference_url, "reference")
    try:
        expected = read_schema(ref_conn)
    finally:
        ref_conn.close()

    if not expected:
        raise SystemExit(
            "check_schema: the reference database has no tables in `public`\n"
            "\n"
            "infra/sql/init.sql was evidently not applied to it. Comparing against an empty\n"
            "reference would report every target object as an extra and find nothing missing,\n"
            "which is a green run that checked nothing.\n"
        )

    target_conn = _connect(target_dsn, "target")
    try:
        actual = read_schema(target_conn)
    finally:
        target_conn.close()

    drift = compare(expected, actual)
    print(json.dumps(drift.to_dict(), indent=2) if args.json else render(drift, expected=expected))
    if not drift.ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
