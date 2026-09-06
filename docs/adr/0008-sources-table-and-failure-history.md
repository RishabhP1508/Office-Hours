# 0008: A `sources` table, and a broken/overdue distinction, ahead of the gateway

## Context

Through Phase 6, `documents` carried `source_url`, `resolved_url`, `page_last_updated`,
`fetched_at`, and `last_verified_at` on every chunk row. Every chunk from the same source repeated
the same four values -- 221 rows for 14 sources meant the same crawl bookkeeping copied once per
chunk that source happened to produce, from 4 times (`traveling-as-an-international-student`) to 60
(`ice.gov/sevis/travel`), not some fixed multiple. That was tolerable while the only
consumer was `GET /sources/status` aggregating across all 221 rows to find the weakest link, but it
had a real gap: a source that failed to fetch produced no new chunk at all, so there was no row
anywhere to record that it had failed, how many times, or since when. `app/recrawl.py`'s refresh job
already retried a bounded number of times and recorded `fetch_failed` in its own run report, but that
report is a GitHub Actions artifact, not queryable state -- nothing in the database remembered that a
source was stuck failing once the run ended.

## Decision

`sources` (`infra/sql/init.sql`) becomes one row per manifest entry: `resolved_url`,
`page_last_updated`, `fetched_at`, and `last_verified_at` move here from `documents`, plus new
columns `last_changed_at`, `change_count`, `consecutive_failures`, `last_error`, `last_http_status`,
`last_success_at`, and `status` (`ok` / `fetch_failed` / `robots_disallowed`). `documents.source_url`
becomes a foreign key into `sources`, `ON DELETE RESTRICT ON UPDATE CASCADE` -- RESTRICT, not
CASCADE, because CASCADE would let deleting a `sources` row silently delete every chunk that cites
it with nothing re-inserted to replace them, and `app/ingest.py::_embed_and_store` already
guarantees a source row is upserted before its chunks are touched and its chunks are always
replaced, never merely deleted, in one transaction. `rule_effective_date` stays on `documents`: it is
a per-chunk curator annotation, not per-source bookkeeping, and a single source page can carry
several chunks that each state a different dated rule.

A new `record_source_failure` (`app/recrawl.py`) is the write path a failed crawl now has that it
never had before: it increments `consecutive_failures` and records `last_error`/`last_http_status`
for a transient failure, or sets `status = 'robots_disallowed'` without touching the failure count
at all for a permanent one (robots.txt disallowing a URL is not a count of attempts, it is a
standing fact). It is written to never advance `last_verified_at`, `last_success_at`, `fetched_at`,
`last_changed_at`, or `change_count` -- a failed attempt checked nothing and fetched nothing, so none
of those clocks may move, which is what lets `GET /sources/status`'s existing `min()`-based freshness
indicator keep aging correctly on its own for a source stuck failing, instead of a bookkeeping write
disguising a failure as a successful check.

`source_health_state` (`app/guardrails/freshness.py`, pure, no DB, no clock of its own -- the same
shape `sources_freshness_state` already established) decides BROKEN when any of: `consecutive_failures
>= 3`, or `status == 'robots_disallowed'`, or `last_success_at` is more than 7 days old (or
`None`, meaning never once succeeded). Three consecutive failures is three days running against the
daily refresh cron (`.github/workflows/recrawl.yml`, `17 8 * * *`) -- past where one bad night or a
transient network blip explains it. Seven days is not a new number: it is the existing
`recent`/`stale` boundary `sources_freshness_state` already uses (168 hours), reused rather than
inventing a second, unrelated staleness timescale that could drift out of agreement with the first. A
robots disallow is broken immediately, with no count at all, because it is permanent by nature --
nothing about retrying makes a disallowed URL fetchable again.

`GET /sources/status` reports `broken_source_count` and `broken_sources` (each with its own
`status`/`consecutive_failures`/`last_error`/`last_http_status`/`last_success_at`) alongside the
existing freshness band, computed by calling `source_health_state` per row, never by SQL that
duplicates the rule. The frontend's header (`Header.tsx`) renders a distinct, dot-free message
("N of M sources not reachable[, since DATE]") that takes precedence over the age label when both
apply, reading the decision from the API response rather than recomputing it -- the same discipline
Phase 6 established for the freshness band itself.

## Why this had to land before the gateway

The gateway proxies `GET /sources/status` unchanged: whatever shape that endpoint returns is what the
frontend's header renders through the gateway exactly as it did calling the orchestrator directly.
Landing the schema change and the broken-vs-overdue logic first means the gateway's own work (Stage
B) is purely a proxying and edge-policy concern -- rate limiting, PII redaction, timeouts, tracing --
with no endpoint shape still in flux underneath it. Sequencing it the other way would have meant
building the gateway against a `/sources/status` response that was about to change shape, or shipping
the gateway and then changing what it proxies without the gateway itself needing to know or care --
either way, a clean edge/backend separation reads more clearly when the backend's own data model is
already settled first.

## Tradeoff

`sources` is a genuine normalization, not a superficial rename: existing code that read
`resolved_url`/`page_last_updated`/`fetched_at`/`last_verified_at` off `documents` had to change to
read them off `sources` instead (the hybrid-search query's `SELECT`, `touch_last_verified`,
`reindex_source`, `GET /sources/status`, and every test that inserted a row directly). That is a
real migration cost, accepted once, in exchange for the crawl bookkeeping no longer being duplicated
221 times over and for having anywhere at all to record a fetch failure.

The migration itself carries a real, if narrow, risk: collapsing per-chunk columns to one row per
source requires every chunk from the same source to already agree with itself. The migration's own
guard (`infra/sql/init.sql`'s `DO $$ ... $$` block) checks this and raises loudly rather than
silently picking a value with `max()` if it does not hold -- including the NULL-vs-non-NULL case
`count(DISTINCT ...)` alone would miss, verified directly against a reconstructed pre-Phase-7 schema
before this migration shipped. That guard only runs once, against the one database this repository
has ever had; it is dead code for a schema that starts fresh, kept because a future clone of an
old dump could still hit it.

## Alternatives considered

- **A separate `source_failures` log table, `documents`/`sources` left as they were.** Rejected: a
  log answers "what happened," but `GET /sources/status` and the frontend need "what is true right
  now" -- current `status`, current `consecutive_failures` -- which a log would require aggregating
  on every read instead of reading directly off one row.
- **Recording failures as a special row in `documents` with no chunk content.** Rejected: a `documents`
  row that is not really a chunk (no content, no embedding) would corrupt every query that scans
  `documents` expecting a real, embeddable chunk, and `content_tsv`/the HNSW index would have to
  special-case rows that carry neither.
- **`ON DELETE CASCADE` on the new foreign key, matching a simpler default.** Rejected outright: see
  Decision above. CASCADE is the one choice this ADR treats as not a matter of taste -- it is the
  exact failure mode (chunks deleted with nothing to replace them) this whole normalization exists to
  make structurally impossible, not just avoided by convention.
