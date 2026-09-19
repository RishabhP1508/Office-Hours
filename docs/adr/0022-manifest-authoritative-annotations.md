# 0022: data/sources/sources.yaml is authoritative for curator annotations; sources.rule_effective_date is removed

## Context

`sources.rule_effective_date` (added by ADR 0014, for the stateless recrawl diff) was a `sources`
column that mirrored a curator annotation so `app/recrawl.py::_diff_node` had somewhere to read it
from without a snapshot file on disk. Production was never migrated to add that column. Every
statement that touched it there raised `psycopg.errors.UndefinedColumn`, and because
`record_source_failure` only ever writes the columns `sources` actually has, that failure did not
surface as a schema error. It surfaced as fourteen `fetch_failed` rows, on every source, on every
run, with no indication that the real cause was a missing column rather than fourteen broken pages.

### The database-sourced design was a self-perpetuating loop that locked the curator out

Before this change, the annotation's only writers were `app/recrawl.py::touch_last_verified` and
`::reindex_source`, and its only reader for the diff was `_diff_node` -- and all three read the
value FROM `sources.rule_effective_date` and wrote it straight back to the same column, unchanged,
on every run. There was no path by which editing or deleting the annotation anywhere else (a
snapshot's frontmatter, by hand, or later, the manifest) could reach the database: whatever value
was already stored there just kept re-asserting itself, forever. A curator who wanted to change or
remove `rule_effective_date` had two options, both bad: write raw SQL against production, or run a
full `python -m app.ingest`, which re-embeds every chunk of the source and resets `fetched_at` and
`last_verified_at` -- values `infra/sql/init.sql` documents as "not reconstructable after the fact"
-- for a change to one date. The column was not just missing from production. Even where it existed,
it could not be used the way a curator annotation needs to be used.

### Why the column is deleted rather than finally added to production

The obvious alternative was to add the column production never got, restoring what ADR 0014
originally shipped. That does not fix the loop described above; it only makes the loop run without
raising `UndefinedColumn`. The column would still be the thing every write reads back from and
writes back to, so a curator edit to the manifest would still have no effect once the annotation had
been written once. Making the manifest authoritative and having the refresh job read from it instead
solves the actual problem; adding the missing column does not, and leaves a second source of truth
that the manifest and `sources` could drift apart on. `data/sources/sources.yaml` is now that single
source of truth: the daily re-crawl reads it and writes `documents.rule_effective_date` on every run,
and `python -m app.sync_annotations` gives a curator an immediate path that does not wait for the
next scheduled run.

## Decision

`data/sources/sources.yaml`'s `annotations` block is authoritative for `rule_effective_date`.
`sources.rule_effective_date` is removed from `infra/sql/init.sql`'s declaration. Every read and
write of that column is removed from `app/recrawl.py` and `app/ingest.py`.
`documents.rule_effective_date` is unaffected: it is the per-chunk value citations, answers, and the
temporal guard actually read (`app/db.py` selects `d.rule_effective_date`), and every write onto it
is preserved exactly as it was.

Concretely:

- `app/recrawl.py::_initial_state` seeds `RefreshState["rule_effective_date"]` from the manifest
  entry it is handed, via `manifest_annotation(entry, "rule_effective_date")`, instead of
  initializing it to `None` and waiting for `_diff_node` to fill it in from `sources`.
- `app/recrawl.py::_diff_node` no longer reads or returns `rule_effective_date` at all. The value
  `_initial_state` already put in state survives untouched, because a LangGraph node's return dict is
  merged into state, not substituted for it.
- `app/recrawl.py::touch_last_verified` and `::reindex_source` (via
  `app/ingest.py::_embed_and_store`) write `rule_effective_date` onto `documents` only. Neither
  writes it onto `sources` any longer.
- `app/ingest.py::_ingest_from_manifest` reads the annotation with `manifest_annotation(entry, ...)`
  instead of `_parse_iso_date(frontmatter.get("rule_effective_date"))`.

### Why `manifest_annotation` moved into `app/ingest.py`

The function existed already, in `app/sync_annotations.py`, with a docstring establishing a specific
and easy-to-get-wrong rule: `None` means "the manifest entry has no `annotations` block, or the
block does not set this key," and that has to be indistinguishable from "absent" so that deleting a
key from the manifest reaches the database as `NULL` rather than being silently ignored. When
`app/recrawl.py::_initial_state` needed the identical behavior, there were three options. Import it
from `app.sync_annotations`: rejected, because that module is a hand-run tool
(`app/sync_annotations.py`'s own docstring explains at length why it is synchronous, for a
laptop-driven workflow), and a scheduled job depending on a laptop tool's module is backwards.
Copy the function into `app/recrawl.py`: rejected, because the None-versus-absent distinction is
exactly the kind of subtlety that gets re-derived slightly differently the second time someone
writes it out from memory, and a silent divergence between two copies would be invisible until a
curator hit the one code path that behaved differently. Move it to `app/ingest.py`, which both
`app/recrawl.py` and `app/sync_annotations.py` already import from for other manifest-reading
helpers (`read_manifest`, `_parse_iso_date`): chosen. `app/sync_annotations.py` now imports and
re-exports the name rather than defining its own copy, so `tests/test_sync_annotations.py`'s existing
`from app.sync_annotations import manifest_annotation` keeps working unchanged.

### The `.isoformat()` call, and why it is required

`RefreshState` is a `TypedDict` that LangGraph's checkpointer persists between graph steps, using
ormsgpack, not JSON. The module docstring states the resulting rule plainly: no `datetime`/`date`
objects in state, ever. YAML parses an unquoted date-shaped scalar like `2026-09-15` directly into a
`datetime.date`, and `manifest_annotation` (via `_parse_iso_date`) returns a `date` unchanged when it
is handed one. Without `.isoformat()`, `_initial_state` would put a raw `date` object into
`RefreshState["rule_effective_date"]`. Every existing test in this codebase drives the graph with a
mocked or bypassed checkpointer, so this would not fail today. It is a latent defect that would only
surface the first time a real checkpointer tried to persist a source carrying this annotation, most
likely on the two `fixed_admission` sources, which are the only ones that currently set it, well
after this change had shipped and been forgotten. `_initial_state` converts it explicitly, with a
comment at the call site recording exactly this reasoning, and
`tests/test_freshness.py::test_initial_state_puts_the_manifest_rule_effective_date_in_state_as_a_string`
pins it with an `isinstance` check, not just an equality check, so a regression back to a raw `date`
is caught even though `date(2026, 9, 15) == "2026-09-15"` would already be `False` on its own.

### The `eval/fixtures/sources/` exception

`app/ingest.py::_ingest_from_snapshots` still reads `rule_effective_date` out of a snapshot's own
YAML frontmatter, not from a manifest entry. This is deliberate and does not reopen the problem this
ADR fixes. That path serves only `eval/fixtures/sources/`, the four-file CI fixture corpus (ADR
0004), which has no manifest entry at all: it is self-contained by design and is itself tracked in
git, so the failure mode this ADR addresses (an annotation living only on one laptop, in a
gitignored directory, invisible to review) cannot happen to it. The fixture file and its frontmatter
are already checked in and travel together. `data/sources/sources.yaml`'s own header comment
documents the same exception from the other direction.

### No `DROP COLUMN`

`infra/sql/init.sql` removes the `ADD COLUMN IF NOT EXISTS rule_effective_date DATE` clause from its
`sources` declaration. It does not issue a `DROP COLUMN`. A developer database created before this
change keeps the column as a harmless, nullable leftover: nothing in the codebase reads or writes it
after this change, so its presence cannot break anything. `app/check_schema.py` already anticipated
this exact case in its own docstring before this ADR was written ("after Option B lands,
`sources.rule_effective_date` is exactly this case on every developer database created before that
change") -- it reports the column as an extra, non-fatal drift item and does not fail the check on
it, because only a *missing* column is a guaranteed runtime failure; an unreferenced extra column is
reported so it is visible, not treated as an error a developer has to act on.

## Tradeoff

A stale developer database now silently disagrees with `infra/sql/init.sql` about one column,
indefinitely, unless someone runs a manual `ALTER TABLE sources DROP COLUMN rule_effective_date`.
That is accepted rather than fixed by a migration runner this project does not have: writing one to
retire a single harmless nullable column is a larger and riskier piece of infrastructure than the
problem it would solve.

## Alternatives considered

**Add `sources.rule_effective_date` to production instead of removing it (revert to ADR 0014's
original two-column design).** Rejected -- see "Why the column is deleted rather than finally added
to production" above. It fixes the immediate `UndefinedColumn` failure but not the actual defect,
which is that the value's source of truth was a column nothing external could ever change.

**Add the column to production AND make the manifest authoritative, keeping both in sync.**
Rejected. This keeps a column that becomes vestigial the moment the manifest is authoritative --
every write to it would be dead code repeating what `documents.rule_effective_date` already records
-- and creates a second copy the manifest and `sources` could silently drift apart on, which is a
worse failure mode than a stale nullable leftover on old developer databases.

**Copy `manifest_annotation` into `app/recrawl.py` instead of moving it.** Rejected -- see "Why
`manifest_annotation` moved into `app/ingest.py`" above. Two copies of a rule this specific is a
standing invitation for the second one to be subtly wrong.

**Convert the annotation's manifest value to a string once, in `read_manifest`, rather than in
`_initial_state`.** Rejected. `manifest_annotation` returning a real `date` is correct and useful
everywhere else it is called (`app/sync_annotations.py` and `app/ingest.py::_ingest_from_manifest`
both want a `date` to pass to a database column typed `DATE`). The JSON/msgpack-serializability
requirement is specific to `RefreshState`, so the conversion belongs at the one call site that
actually needs it, not upstream in a function three other callers use for a different purpose.
