"""One-time backfill: populate `sources.last_indexed_body`/`rule_effective_date` for every source
that was ingested before those two columns existed (infra/sql/init.sql), from the local snapshot
files already sitting in `data/sources/raw/` (RAW_SNAPSHOT_DIR).

WHY THIS EXISTS, AND WHY SKIPPING IT IS DANGEROUS (docs/adr/0014-stateless-recrawl-diff.md):
app/recrawl.py::_diff_node now reads the previous body it diffs a freshly fetched page against
from `sources.last_indexed_body`, never from a snapshot file on disk -- the file is gitignored and
does not survive between runs on a stateless runner (a fresh GitHub Actions checkout, or
production with no persistent volume), which is exactly what made the scheduled refresh job unable
to run in production at all before this change.

A source ingested before this column existed has `last_indexed_body = NULL`. `_diff_node` refuses
to treat that the same as a genuinely new source: a NULL body on a source that ALREADY has chunks
in `documents` is the unsafe case (see that function's own docstring), and it raises loudly rather
than silently re-indexing and resetting `fetched_at` for a source whose content has not actually
changed. This script is what clears that condition, once, by reading the same local snapshot
files `python -m app.ingest` already wrote and already indexed FROM, and copying each one's body
and `rule_effective_date` frontmatter annotation into the matching `sources` row.

Runnable as:

    python -m app.backfill_source_bodies [--dry-run]

Idempotent and safe to re-run: the UPDATE below only ever touches a row whose `last_indexed_body`
is still NULL. Re-running after a partial run, or after some other process has already legitimately
written a body (an ordinary `python -m app.ingest` re-run, or a real content change the refresh job
already re-indexed), touches nothing that process wrote -- it never overwrites an already-populated
body.

MUST be run against production (with RAW_SNAPSHOT_DIR pointed at a local checkout that still holds
the 14 snapshot files) before the scheduled refresh job runs again there. The user runs this by
hand; nothing in this repository's CI or the scheduled workflow calls it automatically, on purpose
-- see .github/workflows/recrawl.yml's own comments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector_async

from app.config import Settings, get_settings
from app.ingest import _parse_iso_date, load_snapshot

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_source_bodies")


async def backfill_source_bodies(
    conn: psycopg.AsyncConnection, raw_dir: Path, *, dry_run: bool = False
) -> dict[str, int]:
    """Read every `*.md` snapshot in `raw_dir` and, for each one whose `source_url` has a
    `sources` row with `last_indexed_body` still NULL, write that snapshot's body and
    `rule_effective_date` there. Returns counts for the caller to report:

    - `updated`: rows actually written (or, in `--dry-run`, that WOULD be written).
    - `already_populated`: snapshots whose matching `sources` row already had a body -- skipped,
      not overwritten (this is what makes re-running safe).
    - `snapshots_without_a_sources_row`: a snapshot on disk for a source_url with no `sources` row
      at all yet (never ingested) -- nothing to backfill, flagged so a human notices.
    - `sources_rows_still_null_after`: how many `sources` rows have a NULL `last_indexed_body`
      once this call returns (0 in `--dry-run`, since nothing was written) -- a real, non-dry-run
      run leaving this above 0 means some `sources` row has no matching local snapshot at all, and
      _diff_node will still refuse to proceed for it.
    """
    updated = 0
    already_populated = 0
    snapshots_without_a_sources_row = 0

    for path in sorted(raw_dir.glob("*.md")):
        frontmatter, body = load_snapshot(path)
        source_url = frontmatter.get("source_url")
        if not source_url:
            logger.warning("skipping %s: no source_url in frontmatter", path)
            continue
        rule_effective_date = _parse_iso_date(frontmatter.get("rule_effective_date"))

        # The read (does a `sources` row exist, is its body already populated) and the write (the
        # guarded UPDATE) run inside ONE transaction per source, so this is durable the moment the
        # loop moves to the next file -- never left as an uncommitted change riding on whatever
        # ambient transaction a bare execute() outside any `conn.transaction()` block would
        # otherwise leave open until (and only committed by) some later, unrelated call. A `conn`
        # in autocommit mode is unaffected: entering `conn.transaction()` there still issues a real
        # BEGIN/COMMIT around exactly this block.
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT last_indexed_body FROM sources WHERE source_url = %s", (source_url,)
                )
                row = await cur.fetchone()

                if row is None:
                    snapshots_without_a_sources_row += 1
                    logger.warning(
                        "%s: snapshot on disk but no `sources` row for %s -- skipped (never "
                        "ingested?)",
                        path.name,
                        source_url,
                    )
                    continue

                (existing_body,) = row
                if existing_body is not None:
                    already_populated += 1
                    logger.info("%s: already has last_indexed_body -- skipped", source_url)
                    continue

                if dry_run:
                    updated += 1
                    logger.info("[dry-run] would backfill %s from %s", source_url, path.name)
                    continue

                await cur.execute(
                    """
                    UPDATE sources
                    SET last_indexed_body = %s, rule_effective_date = %s
                    WHERE source_url = %s AND last_indexed_body IS NULL
                    """,
                    (body, rule_effective_date, source_url),
                )
                if cur.rowcount == 1:
                    updated += 1
                    logger.info("backfilled %s from %s", source_url, path.name)
                else:
                    # Someone else populated this row between the SELECT above and this UPDATE
                    # (a concurrent ingest/recrawl) -- safe to treat as already_populated, not an
                    # error: the WHERE guard is exactly what stopped this from clobbering it.
                    already_populated += 1
                    logger.info(
                        "%s: populated concurrently -- skipped without overwriting", source_url
                    )

    if dry_run:
        sources_rows_still_null_after = 0
    else:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM sources WHERE last_indexed_body IS NULL")
                (sources_rows_still_null_after,) = await cur.fetchone()

    return {
        "updated": updated,
        "already_populated": already_populated,
        "snapshots_without_a_sources_row": snapshots_without_a_sources_row,
        "sources_rows_still_null_after": sources_rows_still_null_after,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.backfill_source_bodies",
        description=(
            "One-time backfill of sources.last_indexed_body/rule_effective_date from the local "
            "snapshot files in RAW_SNAPSHOT_DIR, so app/recrawl.py's stateless diff has a "
            "baseline for every already-ingested source. See this module's own docstring."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change; write nothing",
    )
    return parser.parse_args(argv)


async def _run(dry_run: bool) -> dict[str, int]:
    settings: Settings = get_settings()
    raw_dir = Path(settings.RAW_SNAPSHOT_DIR)
    conn = await psycopg.AsyncConnection.connect(settings.DATABASE_URL)
    await register_vector_async(conn)
    try:
        return await backfill_source_bodies(conn, raw_dir, dry_run=dry_run)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    counts = asyncio.run(_run(args.dry_run))
    print(json.dumps(counts, indent=2))
    if not args.dry_run and counts["sources_rows_still_null_after"] > 0:
        # A real (non-dry-run) pass left rows still NULL: some `sources` row has no matching local
        # snapshot at all. _diff_node will still refuse to proceed for it -- worth a nonzero exit
        # so a human notices before the next recrawl runs against this database.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
