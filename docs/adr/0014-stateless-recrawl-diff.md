# 0014: The scheduled refresh diffs against the database, not a snapshot file, so it can run on a stateless runner

## Context

`app/recrawl.py`'s daily refresh job is designed to run on a fresh GitHub Actions checkout every
time (`.github/workflows/recrawl.yml`, `actions/checkout@v4`, no persistent volume). Until now,
`_diff_node` compared each freshly fetched page against a snapshot FILE in `data/sources/raw/`,
read via `load_existing_snapshot_index(deps.raw_dir)`. That directory is gitignored on purpose
(the snapshots are large, generated artifacts, not source), which means a fresh checkout starts
every run with nothing in it.

The consequence was not a minor inefficiency. With no file to diff against, every one of the 14
sources fell into `_diff_node`'s `no_existing_snapshot` branch and classified `meaningful`, the same
branch used for a genuinely brand-new source. `_after_diff` routes every `meaningful` verdict to
`_chunk_node` -> `_embed_node` -> `_reindex_node`, which re-embeds the source's content and calls
`reindex_source` with `mark_changed=True`, setting `fetched_at = last_verified_at = now` and
incrementing `change_count`. Run on a fresh checkout, this would re-index and re-embed all 14
sources and overwrite every `fetched_at` with the run's own timestamp, on a day nothing had
actually changed -- silently destroying `fetched_at`'s own meaning ("when the content currently
indexed was downloaded") and the only freshness history this project has, since `fetched_at` and
`last_changed_at` are both explicitly marked "not reconstructable after the fact" in
`infra/sql/init.sql`.

`.github/workflows/recrawl.yml` already had a guard against exactly this ("Guard against a live
run with no baseline snapshots") that checked whether `RAW_SNAPSHOT_DIR` held any `*.md` files
before letting a live run proceed, and refused (`exit 1`) if not. That guard was doing its job
correctly -- it is why the scheduled job has never actually corrupted the corpus -- but its
existence meant the job could never run live at all, on any runner that does not already happen to
have `data/sources/raw/` populated from a previous run. No such runner exists: GitHub Actions
checkouts are fresh every time, and production (Fly) has no persistent volume mounted at that path
either. The runner was stateless; the differ was not. That mismatch, not the guard, was the actual
defect.

## Decision

Move the diff's baseline from a file to two new columns on `sources`
(`infra/sql/init.sql`): `last_indexed_body` (the raw markdown last actually indexed for this
source -- the same text a snapshot file holds, before chunking) and `rule_effective_date` (a mirror
of the curator annotation the differ used to read out of the previous snapshot's frontmatter).
`app/recrawl.py::_diff_node` now reads both from the database instead of from disk, and never opens
`RAW_SNAPSHOT_DIR` for that purpose again. `app/ingest.py::_embed_and_store` -- the one function
both a first-time `python -m app.ingest` and `app/recrawl.py::reindex_source`'s meaningful-change
path funnel every chunk write through -- now also writes `last_indexed_body`/`rule_effective_date`
onto `sources` in the same upsert it already does for the rest of that source's bookkeeping. An
unchanged/cosmetic verdict (`touch_last_verified`) never touches `last_indexed_body`, for the same
reason `fetched_at` does not move there either: nothing was re-indexed, so the next run's baseline
must stay exactly what it already was.

Fetch, snapshot, and chunk stay exactly as they were (ARCHITECTURE.md, "Fetch, snapshot, and chunk
are three separate steps"): a meaningful change still writes today's snapshot file and chunks it
from that file within the same run. Only the diff's source of the PREVIOUS run's content moved --
what a run no longer needs is a file some earlier run left behind.

### The backfill is the dangerous part, not a footnote

Every `sources` row that existed before this migration has `last_indexed_body = NULL`. If
`_diff_node` treated that NULL the same way it treats a source that has genuinely never been
indexed, the very first production run under this change would reproduce precisely the disaster
this ADR exists to prevent -- for all 14 real sources, not zero. `_diff_node` distinguishes the two
cases by checking whether `documents` already holds chunks for that `source_url`: no baseline body
and no existing chunks means a genuinely new source (proceed exactly as `no_existing_snapshot`
always has); no baseline body but chunks already present means the backfill has not run yet, and
`_diff_node` raises rather than proceeding. That exception surfaces through `run_refresh`'s existing
per-source isolation (the same try/except that already turns any other node failure into a reported
`fetch_failed`), so one un-backfilled source fails loudly and turns the run red without silently
touching that source's freshness clocks or stopping the rest of the run.

`app/backfill_source_bodies.py` (`python -m app.backfill_source_bodies [--dry-run]`) is the
one-time, idempotent command that clears that condition: it reads the same local snapshot files
`python -m app.ingest` already indexed FROM, and writes each one's body and `rule_effective_date`
into the matching `sources` row -- but only where `last_indexed_body IS NULL`, so a re-run never
overwrites a body some other process (an ordinary re-ingest, or a real recrawl-driven re-index
since) has already legitimately written.

### Reconstruction from `documents` was investigated and ruled out

The obvious alternative to a backfill script -- derive the previous body from the chunks already
stored in `documents`, since they are already in the database -- was measured, not assumed, and
rejected. For one real source, concatenating its stored chunks back into a "body" gives 12,574
characters against the original snapshot's 12,321. The gap is `chunk_markdown`'s own breadcrumb
line, prefixed onto every chunk's stored text so the embedding and the model can see which group a
question belongs to (ARCHITECTURE.md, "Parenting comes from document order..."). `classify_change`
is line-based (`normalize_for_diff` + a line-by-line comparison), so those inserted breadcrumb lines
would read as an added line at every single chunk boundary, and `classify_change` would report
`meaningful` for every source on the very first stateless run -- the identical failure mode this
whole change exists to close, just reached by a different route. There is no cheap fix for this:
undoing the breadcrumb prefix loses the parent/heading structure it exists to carry, and chunk text
is not stored separately from it anywhere.

## Tradeoff

Two new nullable columns on `sources`, both write-once-per-index rather than derived on read, cost
238 KB across the 14 real sources today (the snapshot bodies are small; storing them in Postgres is
not the expensive part of this change). The real cost is operational, not storage: the backfill is
a manual step the user has to run, once, against production, before the very first recrawl under
this code, and forgetting it is exactly the failure this ADR is about -- mitigated by making
`_diff_node` refuse rather than proceed silently, but not eliminated as a step someone has to
remember to take.

A second, smaller cost: the curator workflow for `rule_effective_date` used to be "edit the
snapshot file's frontmatter by hand, then let the next recrawl pick it up automatically," because
`_diff_node` re-read whatever frontmatter was CURRENTLY on disk on every run, curator edits
included. On a stateless runner that file does not persist between runs, so that automatic pickup
path is gone. Updating `rule_effective_date` going forward means writing `sources` (and, to keep
citations consistent, `documents`) directly, or re-running a local `python -m app.ingest` and
propagating its result to production by hand -- not solved by this change, and worth flagging
honestly rather than glossing over.

## Alternatives considered

- **Commit `data/sources/raw/` snapshots to the repository.** Rejected. For a single maintainer,
  this means either a bot with write access to push directly to a protected `main`, or a pull
  request opened and merged for every content change a daily cron finds -- real infrastructure
  (a bot identity, a token, branch-protection carve-outs) for a project whose review signal for a
  meaningful change already exists in a cheaper form: `docs/adr/0009-golden-impact-gates-recrawl-red.md`'s
  golden-impact reporting already tells a human which meaningful changes are worth looking at,
  without needing the raw diff committed anywhere for them to do it.
- **Object storage (S3-compatible bucket) for the snapshots.** Rejected. This project has spent a
  phase (Phase 7/8) deliberately narrowing its infrastructure to Postgres, Redis, and the two model
  providers; a bucket is a new vendor, a new credential, and a new failure mode (a fetch that
  succeeds against the origin but fails against the bucket) for a problem two nullable columns on a
  table this job already writes to solves without adding anything.
- **Run the refresh from a developer's own machine on a schedule instead of GitHub Actions.**
  Rejected. The entire point of `fetched_at`/`last_verified_at` is an honest freshness claim; making
  that claim depend on a laptop being powered on and network-reachable at a particular hour is a
  worse guarantee than the one being fixed, not a better one.
- **Reconstruct the previous body from `documents` chunks at diff time, with no new column at
  all.** Investigated and rejected -- see "Reconstruction from `documents` was investigated and
  ruled out" above. The 12,574-vs-12,321-character measurement is the reason this was not simply
  assumed to be fine.
