"""Push curator annotations from data/sources/sources.yaml into the database, with no fetch, no
chunking and no re-embedding.

WHY THIS EXISTS. `data/sources/sources.yaml` is authoritative for the curator annotations (see that
file's own header and docs/adr/0014-stateless-recrawl-diff.md's amendment). The scheduled re-crawl
already carries them into `sources` and `documents` on every run, for every source, changed or not
(app/recrawl.py::touch_last_verified and ::reindex_source). That is the normal propagation path and
it needs nothing from this module.

This module is the path for the case the re-crawl cannot serve: a curator changes a value and needs
it live NOW, without waiting for the next daily tick and without the tick necessarily working. The
alternative today is a full `python -m app.ingest`, which re-embeds every chunk of the source and
resets `fetched_at`/`last_verified_at`, destroying freshness history that infra/sql/init.sql marks
"not reconstructable after the fact" -- for a change to one date. Or hand-written SQL against
production. Both are worse than a tool whose blast radius is written down.

WHY THIS IS NOT PART OF app/backfill_source_bodies.py, which is a similar shape. Two reasons, both
load-bearing. (1) That script's `WHERE ... AND last_indexed_body IS NULL` guard is what makes it
idempotent and safe to re-run, and is documented as such; updating an annotation on an
already-populated row means relaxing exactly that guard. (2) That script writes only `sources`,
while retrieval reads `documents.rule_effective_date` (app/db.py selects `d.rule_effective_date`),
so a tool that wrote only `sources` would look like it worked and change nothing any guardrail can
see. The correct model is `touch_last_verified`, which writes both tables, not the backfill.

WHY THIS IS SYNCHRONOUS while everything around it is async. app/ingest.py and app/recrawl.py are
async because they do concurrent I/O: HTTP fetches, embedding calls, a LangGraph state machine.
This tool issues a handful of SELECTs and UPDATEs, strictly sequentially, and has nothing to
overlap. Being async bought it nothing and cost it a hard failure: on Windows, Python defaults
asyncio to the ProactorEventLoop, and psycopg refuses to run in async mode on that loop
(`InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode`), raised before
a single packet is sent. That is a blocking defect for a tool whose whole purpose is to be reached
for by hand, from a laptop, when something needs changing quickly.

The error message suggests installing WindowsSelectorEventLoopPolicy. Do not: that call, and the
whole event-loop policy system, are deprecated as of 3.14 and slated for removal in 3.16, so on the
interpreter this is most likely to be hand-run from, the suggested fix emits a DeprecationWarning.
The correct async workaround is `asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)`
(`loop_factory` reached `asyncio.run` in 3.12, matching this project's pin), and it was verified to
work. It is still a workaround for concurrency this tool does not have, so the event loop was
removed instead of accommodated. A synchronous psycopg connection reaches the network with no loop
involved on any platform or version. It also needs no `register_vector_async`, unlike its async
neighbours, because it touches no vector column.

THE BLAST RADIUS, stated exhaustively because this is the first tool outside app/ingest.py and
app/recrawl.py to write `documents` at all:

    WRITES, and nothing else:
        documents.rule_effective_date               (N rows per annotated source, one per chunk)
        documents.rule_status                       (same N rows, same one UPDATE -- see below)
        documents.rule_status_source                (same N rows, same one UPDATE -- see below)
        documents.rule_status_source_evidences_status (same N rows, same one UPDATE -- see below)

    READS ONLY:
        documents.rule_effective_date, documents.rule_status, documents.rule_status_source,
        documents.rule_status_source_evidences_status
                                         (to compute the before/after diff, and to tell a source
                                         that is absent from the corpus from one that is present)

    TOUCHES THE `sources` TABLE NOT AT ALL. It used to write `sources.rule_effective_date` and
    read the before-value from there. Both were removed on 19 September 2026 under Option B: that
    column is never being added to production, so every statement against it raised
    UndefinedColumn and the tool could not run. None of the other three has ever had a `sources`
    mirror at all. `documents` is the one table retrieval actually reads (app/db.py selects
    `d.rule_effective_date`, `d.rule_status`, `d.rule_status_source`,
    `d.rule_status_source_evidences_status`), and was always the load-bearing copy.

    NEVER TOUCHED, by construction -- these columns appear in no UPDATE in this file:
        every column of `sources` without exception, plus documents.content,
        documents.embedding, documents.section_heading, documents.heading_level

No row is ever INSERTed and no row is ever DELETEd. A manifest entry whose `source_url` has no
`sources` row is reported and skipped, never created: this tool syncs annotations onto an existing
corpus and is not an ingest.

ALL FOUR ANNOTATIONS WITH A DATABASE COLUMN ARE SYNCED TOGETHER, in one UPDATE per source
(2026-09-19, docs/adr/0023-curator-rule-status.md -- this docstring used to say a fourth annotation
gaining a column would need this treatment; `rule_status_source_evidences_status` is that fourth
one, added the same day `rule_status_source` itself gained the requirement that it never travel
without one). `federal_register`, `heading_note` and `note` remain human-facing and live in the
manifest only; nothing in services/ or eval/ reads them. Each is also VALIDATED here, at sync time,
the same way app/ingest.py and app/recrawl.py validate at load time (`app/rule_status.py::
validate_rule_status`) -- a curator's manifest edit that would be a hard error at the next ingest
is refused here too, immediately, rather than accepted and left for the next full re-ingest to
discover. When a FIFTH annotation gains a column, it has to be added to the one UPDATE/SELECT pair
below AND to the blast-radius list above, which stops being true the moment a column is written
that it does not name.

Runnable as:

    python -m app.sync_annotations --dry-run          # report the diff, write nothing
    python -m app.sync_annotations                    # apply it
    python -m app.sync_annotations --only <url>       # restrict to one manifest entry (repeatable)

Idempotent: a source whose stored value already equals the manifest value is skipped without an
UPDATE, so re-running writes nothing and the second run's report reads "0 changed".
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import psycopg

from app.config import Settings, get_settings
from app.ingest import (
    manifest_annotation,
    manifest_annotation_bool,
    manifest_annotation_str,
    read_manifest,
)
from app.rule_status import validate_rule_status

# `manifest_annotation` is re-exported from here (rather than only importable from app.ingest)
# because it is public API of this module today -- tests/test_sync_annotations.py, and any other
# caller written before 19 September 2026, imports it as `from app.sync_annotations import
# manifest_annotation`. The single definition now lives in app/ingest.py (see that function's own
# docstring for why it moved); this import keeps the old spelling working without a second copy of
# the logic. `manifest_annotation_str` (`rule_status`/`rule_status_source`, plain strings, no date
# parsing) is the same shape of import, added 19 September 2026 alongside the second and third
# synced annotation. `manifest_annotation_bool` (`rule_status_source_evidences_status`, an
# uncoerced value so a non-boolean curator mistake still raises) is the same shape again, added
# alongside the fourth.

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sync_annotations")

# Deliberately NOT a list of synced annotation names here. There was one, and it was decorative:
# nothing read it, while the real list lived in the SELECT/UPDATE statements below, so a reader
# could have added a name to it and believed something would happen. `rule_effective_date`,
# `rule_status`, `rule_status_source`, and `rule_status_source_evidences_status` are the four
# annotations with a database column, each is named explicitly at each site that uses it, and the
# module docstring says what to do when a fifth arrives.


#: `(rule_effective_date, rule_status, rule_status_source,
#: rule_status_source_evidences_status)`, in the fixed column order every SELECT and UPDATE in this
#: module uses -- one small tuple rather than four parallel scalar fields on `SourceDiff`, so a
#: before/after pair can be compared, logged, and passed to `set(...)` (the mixed-chunk check below)
#: as a single unit, the same way the four columns move as a unit in the one UPDATE that actually
#: writes them.
Annotations = tuple[date | None, str | None, str | None, bool | None]

_NULL_ANNOTATIONS: Annotations = (None, None, None, None)


def _render_annotations(values: Annotations) -> str:
    """Log-friendly rendering of an `Annotations` tuple: `NULL` when all four are unset (the
    common case for 12 of 14 sources), otherwise `date|status|status_source|evidences` with each
    empty slot shown as `-` so a partial annotation (a status with no date, or vice versa) is never
    mistaken for a fully-NULL one at a glance. `rule_status_source_evidences_status` renders as the
    literal string `"True"`/`"False"` (via `str()`), never as `-`, unless it is actually `None` --
    `-` must mean "unset", not "False", or a curator reading this log could not tell the two apart.
    """
    rule_effective_date, rule_status, rule_status_source, rule_status_source_evidences_status = (
        values
    )
    if values == _NULL_ANNOTATIONS:
        return "NULL"
    return "|".join(
        part if part is not None else "-"
        for part in (
            rule_effective_date.isoformat() if rule_effective_date else None,
            rule_status,
            rule_status_source,
            (
                None
                if rule_status_source_evidences_status is None
                else str(rule_status_source_evidences_status)
            ),
        )
    )


def _annotations_to_dict(values: Annotations) -> dict:
    rule_effective_date, rule_status, rule_status_source, rule_status_source_evidences_status = (
        values
    )
    return {
        "rule_effective_date": rule_effective_date.isoformat() if rule_effective_date else None,
        "rule_status": rule_status,
        "rule_status_source": rule_status_source,
        "rule_status_source_evidences_status": rule_status_source_evidences_status,
    }


@dataclass
class SourceDiff:
    """What this tool would do, or did, to one manifest entry. `before`/`after` each carry all
    four synced annotations together (`Annotations`), since 2026-09-19 -- see that type's own
    comment for why a single tuple, not four parallel fields.
    """

    source_url: str
    before: Annotations = _NULL_ANNOTATIONS
    after: Annotations = _NULL_ANNOTATIONS
    changed: bool = False
    documents_rows: int = 0
    # No chunks in `documents` for this source_url: it is in the manifest but not in the corpus.
    # Renamed from `missing_sources_row` on 19 September 2026, when the read moved off `sources`
    # onto `documents` -- the old name described a `sources` row this tool no longer looks at.
    not_in_corpus: bool = False
    # True when this source's chunks do NOT all carry the same (rule_effective_date, rule_status,
    # rule_status_source, rule_status_source_evidences_status) tuple. Impossible to observe while
    # the value was read from `sources` (one row, one value), and worth surfacing rather than
    # silently flattening: it means a previous partial write left the source split.
    before_is_mixed: bool = False


@dataclass
class SyncReport:
    diffs: list[SourceDiff] = field(default_factory=list)

    @property
    def changed(self) -> list[SourceDiff]:
        return [d for d in self.diffs if d.changed]

    @property
    def missing(self) -> list[SourceDiff]:
        return [d for d in self.diffs if d.not_in_corpus]

    def to_dict(self) -> dict:
        return {
            "sources_considered": len(self.diffs),
            "changed": len(self.changed),
            "unchanged": len(self.diffs) - len(self.changed) - len(self.missing),
            "not_in_corpus": len(self.missing),
            "documents_rows_updated": sum(d.documents_rows for d in self.diffs),
            "details": [
                {
                    "source_url": d.source_url,
                    "before": _annotations_to_dict(d.before),
                    "after": _annotations_to_dict(d.after),
                    "changed": d.changed,
                    "documents_rows": d.documents_rows,
                    "not_in_corpus": d.not_in_corpus,
                    "before_is_mixed": d.before_is_mixed,
                }
                for d in self.diffs
            ],
        }


def sync_annotations(
    conn: psycopg.Connection,
    manifest: list[dict],
    *,
    dry_run: bool = False,
) -> SyncReport:
    """Sync `rule_effective_date`, `rule_status`, `rule_status_source`, and
    `rule_status_source_evidences_status` from `manifest` onto the matching `documents` rows -- all
    four together, in one UPDATE per source, since they are one curator annotation, not four
    independent ones (2026-09-19, docs/adr/0023-curator-rule-status.md).

    Each entry's desired values are VALIDATED (`app/rule_status.py::validate_rule_status`) before
    anything is read or written for it -- a manifest mistake (a date with no status, an unknown
    status, a state/date mismatch, a missing `rule_status_source` for `enjoined`/`not_in_force`, or
    a `rule_status_source` with no or non-boolean `rule_status_source_evidences_status`) is refused
    here exactly as loudly as it would be at the next full ingest, rather than pushed to production
    and left for that later run to catch.

    One transaction per source, wrapping the read and the write, so each source is durable the
    moment the loop moves on rather than riding an ambient transaction that some later unrelated
    call would have to commit. This is the same reasoning app/backfill_source_bodies.py documents
    for its own per-source `conn.transaction()` block, and it holds on an autocommit connection
    too: entering the block still issues a real BEGIN/COMMIT around exactly these statements.
    """
    report = SyncReport()

    for entry in manifest:
        source_url = entry["url"]
        desired_date = manifest_annotation(entry, "rule_effective_date")
        desired_status = manifest_annotation_str(entry, "rule_status")
        desired_status_source = manifest_annotation_str(entry, "rule_status_source")
        desired_status_source_evidences_status = manifest_annotation_bool(
            entry, "rule_status_source_evidences_status"
        )
        validate_rule_status(
            source_url=source_url,
            rule_status=desired_status,
            rule_effective_date=desired_date,
            rule_status_source=desired_status_source,
            rule_status_source_evidences_status=desired_status_source_evidences_status,
            today=date.today(),
        )
        desired: Annotations = (
            desired_date,
            desired_status,
            desired_status_source,
            desired_status_source_evidences_status,
        )
        diff = SourceDiff(source_url=source_url, after=desired)

        with conn.transaction():
            with conn.cursor() as cur:
                # Reads `documents`, not `sources`. That is not a stylistic choice: under Option B
                # none of these four columns is ever added to production `sources`, so a SELECT
                # against it raises UndefinedColumn and this tool could not run at all. `documents`
                # is also the correct place to read from on the merits -- it is what retrieval
                # actually uses (app/db.py selects `d.rule_effective_date`, `d.rule_status`,
                # `d.rule_status_source`, `d.rule_status_source_evidences_status`), so it is the
                # value whose before-and-after a curator cares about.
                cur.execute(
                    "SELECT DISTINCT rule_effective_date, rule_status, rule_status_source, "
                    "rule_status_source_evidences_status FROM documents WHERE source_url = %s",
                    (source_url,),
                )
                existing = [tuple(row) for row in cur.fetchall()]

                if not existing:
                    diff.not_in_corpus = True
                    report.diffs.append(diff)
                    logger.warning(
                        "%s: in the manifest but has no chunks in `documents` -- skipped, NOT "
                        "created (this tool annotates an existing corpus, it is not an ingest)",
                        source_url,
                    )
                    continue

                # Normally every chunk of a source carries the same tuple, so `existing` holds
                # one value. More than one means a previous write reached only part of the source;
                # the tool treats that as needing a sync (the set is not {desired}) and says so,
                # rather than picking one of them to call "before".
                diff.before_is_mixed = len(existing) > 1
                diff.before = existing[0] if len(existing) == 1 else _NULL_ANNOTATIONS
                current = diff.before

                if set(existing) == {desired}:
                    report.diffs.append(diff)
                    logger.info(
                        "%s: already %s -- no write", source_url, _render_annotations(desired)
                    )
                    continue

                diff.changed = True
                if dry_run:
                    # Still inside the transaction, but only a SELECT ran. Nothing to roll back.
                    cur.execute(
                        "SELECT count(*) FROM documents WHERE source_url = %s", (source_url,)
                    )
                    (diff.documents_rows,) = cur.fetchone()
                    report.diffs.append(diff)
                    logger.info(
                        "[dry-run] %s: %s -> %s (%d documents rows)",
                        source_url,
                        _render_annotations(current),
                        _render_annotations(desired),
                        diff.documents_rows,
                    )
                    continue

                # ONE table, ONE UPDATE, all four columns together -- see the module docstring's
                # BLAST RADIUS. The `UPDATE sources SET rule_effective_date` that used to run here
                # was removed on 19 September 2026: under Option B that column never arrives in
                # production, and `documents` is the one retrieval reads. Same removal, and the
                # same reason, as app/backfill_source_bodies.py's.
                cur.execute(
                    "UPDATE documents SET rule_effective_date = %s, rule_status = %s, "
                    "rule_status_source = %s, rule_status_source_evidences_status = %s "
                    "WHERE source_url = %s",
                    (*desired, source_url),
                )
                diff.documents_rows = cur.rowcount
                report.diffs.append(diff)
                logger.info(
                    "%s: %s -> %s (%d documents rows)",
                    source_url,
                    _render_annotations(current),
                    _render_annotations(desired),
                    diff.documents_rows,
                )

    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.sync_annotations",
        description=(
            "Push curator annotations from data/sources/sources.yaml into documents. "
            "No fetch, no chunking, no re-embedding. See this module's own docstring for the "
            "exhaustive list of columns it writes and never writes."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="report the diff; write nothing")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="URL",
        help="restrict to one manifest entry (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser.parse_args(argv)


def _mask_dsn(dsn: str) -> str:
    """A connection string safe to print: the password becomes `***`, everything else is kept so
    the host and database are diagnosable. Never print `settings.DATABASE_URL` raw -- the whole
    point of these error paths is that a person runs this by hand and pastes the output somewhere.
    """
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)


def _load_manifest(settings: Settings) -> list[dict]:
    """Read the manifest, or exit with an explanation instead of a FileNotFoundError three frames
    deep.

    `Settings.SOURCES_MANIFEST_PATH` defaults to `/app/data/sources/sources.yaml`, which is the path
    inside the orchestrator's Docker image and exists in no checkout. That default is correct for
    the service and wrong for every hand-run invocation, so the unset case is the common one here
    and deserves a real message rather than a traceback.
    """
    path = Path(settings.SOURCES_MANIFEST_PATH)
    try:
        return read_manifest(path)
    except FileNotFoundError:
        origin = (
            "(set in the environment)"
            if "SOURCES_MANIFEST_PATH" in os.environ
            else "(unset, so the container default was used)"
        )
        guess = Path(__file__).resolve().parents[3] / "data" / "sources" / "sources.yaml"
        raise SystemExit(
            f"sync_annotations: manifest not found\n"
            f"\n"
            f"  looked for : {path}\n"
            f"  from       : SOURCES_MANIFEST_PATH {origin}\n"
            f"\n"
            f"That default is the path inside the orchestrator Docker image and exists in no\n"
            f"checkout. Point it at this checkout's manifest, for example:\n"
            f"\n"
            f'  SOURCES_MANIFEST_PATH="{guess}" python -m app.sync_annotations --dry-run\n'
        ) from None


def _connect(settings: Settings) -> psycopg.Connection:
    """Open the connection, or exit with an explanation naming DATABASE_URL.

    Synchronous on purpose; see this module's docstring for why this tool is not async.
    """
    dsn = settings.DATABASE_URL
    if not dsn:
        raise SystemExit(
            "sync_annotations: DATABASE_URL is empty\n"
            "\n"
            "Set it to the database to sync against. Production is a hosted Postgres; the local\n"
            "docker-compose one is postgresql://officehours:officehours@localhost:5432/officehours\n"
            "from the host (the `postgres` hostname only resolves inside the compose network).\n"
        )
    try:
        # A bounded connect, because this is a hand-run tool. Without it, a host that DROPS packets
        # rather than refusing them (a firewalled production database, a VPN that is not up) hangs
        # with no output at all: measured at roughly two minutes against an unroutable address
        # while writing this. Ten seconds is long enough for a hosted database over a slow link and
        # short enough that a person does not assume the tool is working.
        return psycopg.connect(dsn, connect_timeout=10)
    except psycopg.Error as exc:
        raise SystemExit(
            f"sync_annotations: could not connect to the database\n"
            f"\n"
            f"  DATABASE_URL : {_mask_dsn(dsn)}\n"
            f"  error        : {type(exc).__name__}: {str(exc).strip()}\n"
            f"\n"
            f"Check DATABASE_URL points at a reachable database. `@postgres:5432` is the\n"
            f"docker-compose service hostname and resolves only inside that network; from\n"
            f"the host use localhost:5432, or point this at production.\n"
        ) from None


def _run(dry_run: bool, only: list[str] | None) -> SyncReport:
    settings: Settings = get_settings()
    manifest = _load_manifest(settings)
    if only:
        wanted = set(only)
        manifest = [e for e in manifest if e["url"] in wanted]
    conn = _connect(settings)
    try:
        return sync_annotations(conn, manifest, dry_run=dry_run)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    report = _run(args.dry_run, args.only)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        counts = report.to_dict()
        print(
            f"{counts['sources_considered']} considered, {counts['changed']} "
            f"{'would change' if args.dry_run else 'changed'}, {counts['unchanged']} unchanged, "
            f"{counts['not_in_corpus']} not in the corpus, "
            f"{counts['documents_rows_updated']} documents rows"
        )

    if report.missing:
        # A manifest entry with no `sources` row means the corpus and the manifest disagree about
        # what exists. Nonzero so it is noticed rather than read past.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
