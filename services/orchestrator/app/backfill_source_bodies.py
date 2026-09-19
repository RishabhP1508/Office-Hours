"""One-time backfill: populate `sources.last_indexed_body` for every source that was ingested
before that column existed (infra/sql/init.sql), from the local snapshot files already sitting in
`data/sources/raw/` (RAW_SNAPSHOT_DIR).

WRITES EXACTLY ONE COLUMN. It used to write `sources.rule_effective_date` too; that half was
removed on 19 September 2026 as the first piece of Option B, because production is never getting
that column and the two-column form would have failed there. See the UPDATE's own comment, and
REPORT.md, "The first piece of Option B".

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
into the matching `sources` row.

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
import json
import logging
import os
import re
from pathlib import Path

import psycopg

from app.config import Settings, get_settings
from app.ingest import load_snapshot

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_source_bodies")


def backfill_source_bodies(
    conn: psycopg.Connection, raw_dir: Path, *, dry_run: bool = False
) -> dict[str, int | list[str]]:
    """Read every `*.md` snapshot in `raw_dir` and, for each one whose `source_url` has a
    `sources` row with `last_indexed_body` still NULL, write that snapshot's body there -- and
    nothing else. Returns counts for the caller to report:

    - `updated`: rows actually written (or, in `--dry-run`, that WOULD be written).
    - `already_populated`: snapshots whose matching `sources` row already had a body -- skipped,
      not overwritten (this is what makes re-running safe).
    - `snapshots_without_a_sources_row`: a snapshot on disk for a source_url with no `sources` row
      at all yet (never ingested) -- nothing to backfill, flagged so a human notices.
    - `sources_rows_still_null_after`: how many `sources` rows have a NULL `last_indexed_body`
      once this call returns. In `--dry-run` this is a PREDICTION of that number rather than 0 (the
      rows this pass would have filled are subtracted), so the dry run answers the question a person
      actually has before writing anything: which sources will still be unbacked afterwards.
    - `unbacked_source_urls`: those rows NAMED, not merely counted. Above 0 means some `sources`
      row has no matching local snapshot at all, and `_diff_node` will refuse to proceed for each
      one -- so the list is the actionable form and the count is the headline.
    """
    updated = 0
    already_populated = 0
    snapshots_without_a_sources_row = 0
    # Every source_url this pass either wrote or found already populated: the set the
    # dry-run prediction subtracts from the still-NULL rows. See the comment below.
    covered_source_urls: set[str] = set()

    for path in sorted(raw_dir.glob("*.md")):
        frontmatter, body = load_snapshot(path)
        source_url = frontmatter.get("source_url")
        if not source_url:
            logger.warning("skipping %s: no source_url in frontmatter", path)
            continue

        # The read (does a `sources` row exist, is its body already populated) and the write (the
        # guarded UPDATE) run inside ONE transaction per source, so this is durable the moment the
        # loop moves to the next file -- never left as an uncommitted change riding on whatever
        # ambient transaction a bare execute() outside any `conn.transaction()` block would
        # otherwise leave open until (and only committed by) some later, unrelated call. A `conn`
        # in autocommit mode is unaffected: entering `conn.transaction()` there still issues a real
        # BEGIN/COMMIT around exactly this block.
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_indexed_body FROM sources WHERE source_url = %s", (source_url,)
                )
                row = cur.fetchone()

                if row is None:
                    snapshots_without_a_sources_row += 1
                    logger.warning(
                        "%s: snapshot on disk but no `sources` row for %s -- skipped (never "
                        "ingested?)",
                        path.name,
                        source_url,
                    )
                    continue

                # This source has a `sources` row and this pass has dealt with it, whichever branch
                # below it takes. Recorded here rather than in each branch so the dry-run
                # prediction cannot drift out of step with the real path.
                covered_source_urls.add(source_url)

                (existing_body,) = row
                if existing_body is not None:
                    already_populated += 1
                    logger.info("%s: already has last_indexed_body -- skipped", source_url)
                    continue

                if dry_run:
                    updated += 1
                    logger.info("[dry-run] would backfill %s from %s", source_url, path.name)
                    continue

                # WRITES ONE COLUMN. This UPDATE used to write `rule_effective_date` as well, and
                # that half was removed on 19 September 2026 as the FIRST piece of Option B,
                # deliberately ahead of the rest of it. The reason is sequencing, not tidiness:
                # Option B adds only `last_indexed_body` to production and never adds
                # `sources.rule_effective_date`, whose role `data/sources/sources.yaml` takes over.
                # Against that schema the two-column form raises UndefinedColumn and the whole
                # backfill fails -- on the one step that is on the critical path for every option.
                # The alternatives were worse: adding both columns reverts the DDL to Option A and
                # leaves a column step 3 immediately makes vestigial, and adding both then dropping
                # one means doing production DDL twice. See REPORT.md, "The first piece of
                # Option B".
                cur.execute(
                    """
                    UPDATE sources
                    SET last_indexed_body = %s
                    WHERE source_url = %s AND last_indexed_body IS NULL
                    """,
                    (body, source_url),
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

    # NAME the leftovers, never just count them. A count tells you something is wrong; the URLs tell
    # you which sources `_diff_node` is about to raise on, which is the only form of this answer
    # anybody can act on. In a real run the NULL rows ARE exactly the uncovered ones, because the
    # writes have happened; in a dry run nothing was written, so the rows this pass WOULD have
    # filled are subtracted to make the dry run predict the post-run state rather than report a
    # blank 0 (which is what it used to do, and which told the reader nothing at all).
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_url FROM sources WHERE last_indexed_body IS NULL ORDER BY source_url"
            )
            still_null = [row[0] for row in cur.fetchall()]
    if dry_run:
        still_null = [url for url in still_null if url not in covered_source_urls]

    for url in still_null:
        logger.warning(
            "%s: has a `sources` row but no local snapshot -- last_indexed_body stays NULL and "
            "app/recrawl.py::_diff_node will REFUSE to proceed for it",
            url,
        )

    return {
        "updated": updated,
        "already_populated": already_populated,
        "snapshots_without_a_sources_row": snapshots_without_a_sources_row,
        "sources_rows_still_null_after": len(still_null),
        "unbacked_source_urls": still_null,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.backfill_source_bodies",
        description=(
            "One-time backfill of sources.last_indexed_body from the local "
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


def _mask_dsn(dsn: str) -> str:
    """A connection string safe to print: the password becomes `***`, host and database kept so the
    failure is diagnosable. Never print `settings.DATABASE_URL` raw -- this is a hand-run tool and
    its output gets pasted places.
    """
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)


def _resolve_raw_dir(settings: Settings) -> Path:
    """The snapshot directory, or exit explaining which env var to set.

    `Settings.RAW_SNAPSHOT_DIR` defaults to `/app/data/sources/raw`, the path inside the
    orchestrator's Docker image, which exists in no checkout. The glob over a non-existent directory
    would otherwise return nothing and this script would cheerfully report "0 updated" -- a clean
    bill of health for a run that read no files at all, which is the one failure mode a backfill
    must never have.
    """
    raw_dir = Path(settings.RAW_SNAPSHOT_DIR)
    snapshots = sorted(raw_dir.glob("*.md")) if raw_dir.is_dir() else []
    if snapshots:
        return raw_dir

    was_set = "RAW_SNAPSHOT_DIR" in os.environ
    guess = Path(__file__).resolve().parents[3] / "data" / "sources" / "raw"
    reason = "does not exist" if not raw_dir.is_dir() else "exists but holds no *.md snapshots"
    raise SystemExit(
        f"backfill_source_bodies: no snapshots to back fill from\n"
        f"\n"
        f"  looked in : {raw_dir}  ({reason})\n"
        f"  from      : RAW_SNAPSHOT_DIR "
        f"{'(set in the environment)' if was_set else '(unset, container default used)'}\n"
        f"\n"
        f"This script can only read the body it writes from a real snapshot file; see REPORT.md,\n"
        f"'The diff baseline is irreplaceable', for why neither reconstruction from `documents`\n"
        f"nor a fresh fetch is an acceptable substitute. Point it at this checkout, for example:\n"
        f"\n"
        f'  RAW_SNAPSHOT_DIR="{guess}" python -m app.backfill_source_bodies --dry-run\n'
    )


def _connect(settings: Settings) -> psycopg.Connection:
    """Open the connection, or exit explaining it, naming DATABASE_URL.

    Synchronous, and deliberately so. This script previously used `psycopg.AsyncConnection` with no
    concurrency to justify it, which made it unrunnable on Windows: Python defaults asyncio to the
    ProactorEventLoop there and psycopg refuses to run in async mode on it, raised before any packet
    is sent. That is fatal for a tool whose whole job is to be hand-run from the one laptop holding
    the snapshots. `register_vector_async` was dropped rather than converted: this script touches no
    vector column, so the registration was never needed.
    """
    dsn = settings.DATABASE_URL
    if not dsn:
        raise SystemExit(
            "backfill_source_bodies: DATABASE_URL is empty\n"
            "\n"
            "Set it to the database to back fill. This must be the SAME database the snapshots\n"
            "were last ingested into, or the bodies written will not describe what is indexed.\n"
        )
    try:
        # Bounded, because a firewalled host or a down VPN otherwise hangs for about two minutes
        # with no output at all.
        return psycopg.connect(dsn, connect_timeout=10)
    except psycopg.Error as exc:
        raise SystemExit(
            f"backfill_source_bodies: could not connect to the database\n"
            f"\n"
            f"  DATABASE_URL : {_mask_dsn(dsn)}\n"
            f"  error        : {type(exc).__name__}: {str(exc).strip()}\n"
            f"\n"
            f"`@postgres:5432` is the docker-compose service hostname and resolves only inside\n"
            f"that network; from the host use localhost:5432, or point this at production.\n"
        ) from None


def _run(dry_run: bool) -> dict[str, int | list[str]]:
    settings: Settings = get_settings()
    raw_dir = _resolve_raw_dir(settings)
    conn = _connect(settings)
    try:
        return backfill_source_bodies(conn, raw_dir, dry_run=dry_run)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    counts = _run(args.dry_run)
    print(json.dumps(counts, indent=2))
    for url in counts.pop("unbacked_source_urls", []):
        print(f"  still NULL: {url}")
    if not args.dry_run and counts["sources_rows_still_null_after"] > 0:
        # A real (non-dry-run) pass left rows still NULL: some `sources` row has no matching local
        # snapshot at all. _diff_node will still refuse to proceed for it -- worth a nonzero exit
        # so a human notices before the next recrawl runs against this database.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
