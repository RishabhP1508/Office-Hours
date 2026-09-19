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
        documents.rule_effective_date   (N rows per annotated source, one per chunk)

    READS ONLY:
        documents.rule_effective_date   (to compute the before/after diff, and to tell a source
                                         that is absent from the corpus from one that is present)

    TOUCHES THE `sources` TABLE NOT AT ALL. It used to write `sources.rule_effective_date` and
    read the before-value from there. Both were removed on 19 September 2026 under Option B: that
    column is never being added to production, so every statement against it raised
    UndefinedColumn and the tool could not run. `documents.rule_effective_date` is the one
    retrieval actually reads (app/db.py selects `d.rule_effective_date`), and was always the
    load-bearing copy.

    NEVER TOUCHED, by construction -- these columns appear in no UPDATE in this file:
        every column of `sources` without exception, plus documents.content,
        documents.embedding, documents.section_heading, documents.heading_level

No row is ever INSERTed and no row is ever DELETEd. A manifest entry whose `source_url` has no
`sources` row is reported and skipped, never created: this tool syncs annotations onto an existing
corpus and is not an ingest.

ONLY `rule_effective_date` IS SYNCED, because it is the only annotation with a database column.
`federal_register`, `heading_note` and `note` are human-facing and live in the manifest only;
nothing in services/ or eval/ reads them. When a new annotation gains a column (the injunction
status field is the expected next one), it has to be added to the two UPDATE statements below AND
to the blast-radius list above, which stops being true the moment a column is written that it does
not name.

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
from app.ingest import _parse_iso_date, read_manifest

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sync_annotations")

# Deliberately NOT a list of synced annotation names here. There was one, and it was decorative:
# nothing read it, while the real list lived in the two UPDATE statements below, so a reader could
# have added a name to it and believed something would happen. `rule_effective_date` is the only
# annotation with a database column, it is named explicitly at each site that uses it, and the
# module docstring says what to do when a second one arrives.


def manifest_annotation(entry: dict, key: str) -> date | None:
    """The curator value for `key` on one manifest entry, or None when the entry has no
    `annotations` block or does not set that key.

    None is meaningful and is NOT the same as "leave it alone": deleting a key from the manifest is
    how a curator removes an annotation, and that has to reach the database as NULL. This is the
    behaviour the database-sourced path could never provide, because a value already stored there
    would simply re-write itself on every run.
    """
    return _parse_iso_date((entry.get("annotations") or {}).get(key))


@dataclass
class SourceDiff:
    """What this tool would do, or did, to one manifest entry."""

    source_url: str
    before: date | None = None
    after: date | None = None
    changed: bool = False
    documents_rows: int = 0
    # No chunks in `documents` for this source_url: it is in the manifest but not in the corpus.
    # Renamed from `missing_sources_row` on 19 September 2026, when the read moved off `sources`
    # onto `documents` -- the old name described a `sources` row this tool no longer looks at.
    not_in_corpus: bool = False
    # True when this source's chunks do NOT all carry the same rule_effective_date. Impossible to
    # observe while the value was read from `sources` (one row, one value), and worth surfacing
    # rather than silently flattening: it means a previous partial write left the source split.
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
                    "before": d.before.isoformat() if d.before else None,
                    "after": d.after.isoformat() if d.after else None,
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
    """Sync `rule_effective_date` from `manifest` onto the matching `documents` rows.

    One transaction per source, wrapping the read and the write, so each source is durable the
    moment the loop moves on rather than riding an ambient transaction that some later unrelated
    call would have to commit. This is the same reasoning app/backfill_source_bodies.py documents
    for its own per-source `conn.transaction()` block, and it holds on an autocommit connection
    too: entering the block still issues a real BEGIN/COMMIT around exactly these statements.
    """
    report = SyncReport()

    for entry in manifest:
        source_url = entry["url"]
        desired = manifest_annotation(entry, "rule_effective_date")
        diff = SourceDiff(source_url=source_url, after=desired)

        with conn.transaction():
            with conn.cursor() as cur:
                # Reads `documents`, not `sources`. That is not a stylistic choice: under Option B
                # `sources.rule_effective_date` is never added to production, so a SELECT against
                # it raises UndefinedColumn and this tool could not run at all. `documents` is also
                # the correct place to read from on the merits -- it is what retrieval actually
                # uses (app/db.py selects `d.rule_effective_date`), so it is the value whose
                # before-and-after a curator cares about.
                cur.execute(
                    "SELECT DISTINCT rule_effective_date FROM documents WHERE source_url = %s",
                    (source_url,),
                )
                existing = [row[0] for row in cur.fetchall()]

                if not existing:
                    diff.not_in_corpus = True
                    report.diffs.append(diff)
                    logger.warning(
                        "%s: in the manifest but has no chunks in `documents` -- skipped, NOT "
                        "created (this tool annotates an existing corpus, it is not an ingest)",
                        source_url,
                    )
                    continue

                # Normally every chunk of a source carries the same date, so `existing` holds one
                # value. More than one means a previous write reached only part of the source; the
                # tool treats that as needing a sync (the set is not {desired}) and says so, rather
                # than picking one of them to call "before".
                diff.before_is_mixed = len(existing) > 1
                diff.before = existing[0] if len(existing) == 1 else None
                current = diff.before

                if set(existing) == {desired}:
                    report.diffs.append(diff)
                    logger.info(
                        "%s: already %s -- no write",
                        source_url,
                        desired.isoformat() if desired else "NULL",
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
                        current.isoformat() if current else "NULL",
                        desired.isoformat() if desired else "NULL",
                        diff.documents_rows,
                    )
                    continue

                # ONE table. The `UPDATE sources SET rule_effective_date` that used to run here
                # was removed on 19 September 2026: under Option B that column never arrives in
                # production, and `documents` is the one retrieval reads. Same removal, and the
                # same reason, as app/backfill_source_bodies.py's.
                cur.execute(
                    "UPDATE documents SET rule_effective_date = %s WHERE source_url = %s",
                    (desired, source_url),
                )
                diff.documents_rows = cur.rowcount
                report.diffs.append(diff)
                logger.info(
                    "%s: %s -> %s (%d documents rows)",
                    source_url,
                    current.isoformat() if current else "NULL",
                    desired.isoformat() if desired else "NULL",
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
