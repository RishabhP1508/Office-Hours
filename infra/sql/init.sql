-- Office Hours: document store for retrieval.
-- One Postgres instance holds the chunks, the embeddings, and (from Phase 3) the keyword index.
-- No dedicated vector database: see ARCHITECTURE.md "PostgreSQL with pgvector, no dedicated vector database".

CREATE EXTENSION IF NOT EXISTS vector;

-- Phase 7: `sources` normalizes the per-source crawl bookkeeping that used to live duplicated on
-- every chunk row in `documents` (every chunk from the same source_url always carried the same
-- resolved_url/page_last_updated/fetched_at/last_verified_at -- this table makes that one row
-- instead of N identical copies) and adds the failure-history columns the Phase 5 refresh job
-- needs but `documents` had nowhere to put: a source that fails to fetch has no new chunks to
-- attach a status to, but it still needs to be recorded somewhere so app/recrawl.py and
-- GET /sources/status can tell "broken" apart from merely "not yet re-verified".
CREATE TABLE IF NOT EXISTS sources (
    source_url           TEXT PRIMARY KEY,
    -- Where the fetch actually landed after redirects, or NULL when it landed on source_url
    -- itself. Carried across from `documents.resolved_url` unchanged by the migration below; see
    -- ARCHITECTURE.md "Citations point at the resolved URL, not the manifest URL" for why both are
    -- kept.
    resolved_url          TEXT,
    -- The page's own self-reported "last updated"/"last reviewed" date, extracted from its text
    -- (app/ingest.py::extract_page_last_updated). Not reconstructable after the fact: it depends on
    -- the page's wording at the time it was fetched, which a later fetch may have already
    -- overwritten or removed.
    page_last_updated     DATE,
    -- When this source's content was last downloaded. Separate from last_verified_at: a re-crawl
    -- that finds no change updates last_verified_at only (see the ARCHITECTURE.md note this table
    -- carries forward from `documents`). Not reconstructable after the fact -- it is a record of
    -- when a specific HTTP fetch happened, not a value derivable from anything else stored here.
    fetched_at             TIMESTAMPTZ NOT NULL,
    -- When this source was last checked, changed or not. Not reconstructable after the fact, for
    -- the same reason: it is a record of when a specific crawl attempt happened.
    last_verified_at       TIMESTAMPTZ NOT NULL,
    -- When this source's content was last found to have MEANINGFULLY changed (app/recrawl.py's
    -- classify_change returning "meaningful"), as opposed to merely re-verified. NULL only for a
    -- source that has never been re-crawled since its initial ingest. Not reconstructable after the
    -- fact -- there is no other column recording which past crawl was the one that found a real
    -- change.
    last_changed_at        TIMESTAMPTZ,
    -- How many times a re-crawl has found a meaningful change, ever. Not reconstructable after the
    -- fact -- it is a running count of history, not a fact derivable from the source's current row.
    change_count           INT NOT NULL DEFAULT 0,
    -- How many consecutive re-crawl attempts have failed to fetch this source, right up to now.
    -- Resets to 0 on the next successful fetch. This is the signal
    -- app/guardrails/freshness.py::source_health_state uses (alongside `status`) to decide whether a
    -- source is BROKEN, not just overdue for a check. Not reconstructable after the fact -- it
    -- depends on the sequence of past attempts, which nothing else here records.
    consecutive_failures  INT NOT NULL DEFAULT 0,
    -- The most recent fetch failure's error string (e.g. "HTTPStatusError: 404" or
    -- "ConnectTimeout: ..."), or NULL when the most recent attempt succeeded. Not reconstructable
    -- after the fact -- it is a record of what a specific past HTTP attempt actually raised.
    last_error             TEXT,
    -- The most recent fetch's HTTP status code, when the failure carried one (e.g. 404, 500), or
    -- NULL when the failure was a timeout/connection error with no status to report, or when the
    -- most recent attempt succeeded. Kept separate from last_error so "404 vs timeout" is a
    -- queryable fact, not something a caller has to parse back out of an error string.
    last_http_status       INT,
    -- When this source last completed a successful fetch. Distinct from last_verified_at:
    -- last_verified_at moves on every crawl attempt outcome recorded via touch_last_verified or
    -- reindex_source (both success paths), but a source stuck failing every day never gets either
    -- of those calls at all -- last_success_at is what
    -- app/guardrails/freshness.py::source_health_state actually reads to decide "hasn't succeeded
    -- in 7 days", and it is exactly the column record_source_failure must never touch, so a failing
    -- source's verification clock cannot silently advance. Not reconstructable after the fact.
    last_success_at        TIMESTAMPTZ,
    -- "ok": the most recent attempt succeeded (whether or not it found a change).
    -- "fetch_failed": the most recent attempt raised (a timeout, a non-2xx status, ...) -- a
    --   transient condition that may clear on the next attempt.
    -- "robots_disallowed": the most recent attempt found robots.txt disallowing this URL -- a
    --   permanent curation condition (see app/recrawl.py::record_source_failure), never cleared by
    --   retrying, only by a manifest/robots.txt change outside this system's control.
    status                 TEXT NOT NULL DEFAULT 'ok'
        CHECK (status IN ('ok','fetch_failed','robots_disallowed'))
);

COMMENT ON TABLE sources IS
    'One row per manifest entry (data/sources/sources.yaml), holding the crawl bookkeeping that '
    'used to be duplicated across every one of that source''s chunks in `documents`, plus the '
    'failure history `documents` had no row to attach a fetch failure to at all. See each column''s '
    'own comment for why it could not be reconstructed from anything else stored here.';

-- Stateless recrawl (docs/adr/0014-stateless-recrawl-diff.md): the scheduled refresh job
-- (app/recrawl.py) used to diff a freshly-fetched page against a snapshot FILE in
-- data/sources/raw/, which is gitignored -- a fresh checkout (a GitHub Actions runner, or
-- production with no persistent volume) has no such file, so every source read as
-- "meaningful, no_existing_snapshot" and the job could not run there at all without destroying
-- the corpus's freshness history. These two columns move the diff's baseline into the database,
-- which every environment this job runs in already has, so the runner needs nothing left behind
-- by a previous run.
--
-- ADD COLUMN IF NOT EXISTS keeps this file idempotent (re-running it against the already-migrated
-- live database is a no-op for these two lines, same as every other ALTER COLUMN in this file).
-- Both are NULL on every row that existed before this migration -- see
-- app/backfill_source_bodies.py for the one-time, idempotent backfill that must run against
-- production BEFORE the refresh job runs again, and app/recrawl.py::_diff_node for how it refuses
-- to proceed on a source where the backfill has evidently not happened yet.
ALTER TABLE sources
    -- The exact body app/recrawl.py::classify_change last compared THIS source against -- i.e.
    -- the same raw markdown app/ingest.py's snapshot file would have held, before frontmatter,
    -- never a reconstruction from `documents` chunks (a chunk's stored text is prefixed with a
    -- breadcrumb line -- see chunk_markdown -- so concatenating chunks back into a "body" is
    -- lossy in exactly the way that makes the line-based differ mis-classify every chunk boundary
    -- as a change; measured at 12,574 characters of reconstructed text against the real snapshot's
    -- 12,321, see the ADR). Written by app/ingest.py::_embed_and_store on every path that indexes
    -- a source (first ingest and a meaningful re-index alike), so it always reflects exactly what
    -- is currently embedded in `documents` for this source_url. NEVER written on an unchanged or
    -- cosmetic verdict (app/recrawl.py::touch_last_verified) -- exactly the same reason
    -- `fetched_at` does not move on those paths either: this column's meaning is "the body
    -- currently indexed", not "the body most recently fetched".
    ADD COLUMN IF NOT EXISTS last_indexed_body TEXT,
    -- A mirror of the curator annotation app/recrawl.py::_diff_node used to read out of the
    -- PREVIOUS snapshot file's own YAML frontmatter (see documents.rule_effective_date's own
    -- comment below for what the annotation itself means) so the stateless differ has somewhere
    -- to read it from without a file. This does NOT replace documents.rule_effective_date, which
    -- stays the per-CHUNK value actually applied to citations and answers -- this column is the
    -- single per-SOURCE value app/recrawl.py::reindex_source/touch_last_verified apply uniformly
    -- to every one of that source's chunks, which mirrors exactly what those two functions already
    -- did before this column existed (both only ever accepted one `rule_effective_date` for the
    -- whole source, never one per chunk); it exists so that value survives between recrawl runs
    -- without a file to carry it in.
    ADD COLUMN IF NOT EXISTS rule_effective_date DATE;

CREATE TABLE IF NOT EXISTS documents (
    id                 BIGSERIAL PRIMARY KEY,
    content            TEXT NOT NULL,
    source_url         TEXT NOT NULL REFERENCES sources (source_url)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    section_heading    TEXT NOT NULL,
    heading_level      INT NOT NULL,
    embedding          VECTOR(768) NOT NULL
);

-- ON DELETE RESTRICT, not CASCADE: CASCADE would let deleting a `sources` row silently delete every
-- chunk that cites it, with nothing re-inserted to replace them -- an answer's citations would
-- point at rows that no longer exist. RESTRICT means a `sources` row can never be removed while it
-- still has chunks, which is exactly the invariant app/ingest.py::_embed_and_store already
-- maintains procedurally (upsert the source row, then delete-and-replace its chunks, all in one
-- transaction): the source row is always upserted before its chunks are touched, and its chunks are
-- always replaced, never merely deleted, so this constraint should never actually fire in normal
-- operation -- it exists to make that invariant fail loudly if some future code path ever violates
-- it, rather than fail silently by dropping chunks with nothing to replace them.
CREATE INDEX IF NOT EXISTS documents_source_url_idx ON documents (source_url);

COMMENT ON TABLE documents IS
    'Chunk content and embeddings. Per-source crawl bookkeeping (resolved_url, page_last_updated, '
    'fetched_at, last_verified_at) moved to `sources` in Phase 7 -- see that table''s own comments. '
    'rule_effective_date stays here (a per-chunk curator annotation, not per-source bookkeeping); '
    'see its own ALTER TABLE comment below for why it was deliberately left out of that move.';

-- Phase 3: hybrid retrieval. A generated tsvector column for the keyword arm, plus the two indexes
-- that back it and the semantic arm. ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS make this
-- whole file idempotent, so re-running it against the already-ingested live database IS the
-- migration: Postgres backfills content_tsv for every existing row the moment the column is added
-- (it is STORED, not virtual), so applying this file requires no re-ingest and no re-embedding.
--
-- section_heading is weighted 'A' (highest) and content is weighted 'B': a query term that matches
-- the heading a chunk is filed under is a stronger signal of relevance than the same term appearing
-- somewhere in the body, and ts_rank_cd uses the weight to score an 'A' hit higher than a 'B' hit.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', section_heading), 'A') ||
        setweight(to_tsvector('english', content), 'B')
    ) STORED;

CREATE INDEX IF NOT EXISTS documents_content_tsv_gin ON documents USING gin (content_tsv);
CREATE INDEX IF NOT EXISTS documents_embedding_hnsw ON documents USING hnsw (embedding vector_cosine_ops);

-- Phase 5: a known future rule change (e.g. the DHS fixed-period-of-admission final rule, effective
-- Sept 15 2026) is a curator annotation already present in a snapshot's frontmatter
-- (`rule_effective_date: 2026-09-15` on both fixed_admission snapshots) -- lifted here into the
-- table so the TEMPORAL ANSWERS rule (ARCHITECTURE.md: state both the current rule and its dated
-- replacement) is driven by data rather than by the corpus happening to contain both texts.
-- Deliberately NOT moved to `sources` in the Phase 7 normalization below: unlike the four columns
-- that moved, this is a per-CHUNK annotation, not a per-SOURCE one -- a single source page can (and
-- the fixed-admission FAQ does) carry many chunks, and a future source could in principle state more
-- than one dated rule across different sections of the same page. Collapsing it to `sources` would
-- lose that per-chunk granularity for no benefit this phase needs. `sources.rule_effective_date`
-- (added below, for the stateless recrawl diff -- docs/adr/0014-stateless-recrawl-diff.md) does NOT
-- reverse this: it is a separate, later addition holding the single value the refresh job applies
-- uniformly to every chunk of one source (which is all app/recrawl.py has ever done -- it has never
-- carried more than one `rule_effective_date` per source), kept only so that value survives between
-- recrawl runs without a snapshot file to read it from. THIS column stays the one citations and
-- answers actually read.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS rule_effective_date DATE;

-- Phase 7 migration: on a fresh database, `documents` above is already created in its final shape
-- (source_url REFERENCES sources, no legacy columns), so this block is a no-op there -- it only
-- runs against a database still carrying the pre-Phase-7 shape (resolved_url, page_last_updated,
-- fetched_at, last_verified_at as columns directly on `documents`). Guarded on those legacy columns
-- still being present so re-running this file after the migration has already completed is a no-op,
-- keeping the whole file idempotent in both the fresh-database and the already-migrated case.
DO $$
DECLARE
    has_legacy_columns boolean;
    inconsistent_source RECORD;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'documents' AND column_name = 'fetched_at'
    ) INTO has_legacy_columns;

    IF has_legacy_columns THEN
        -- i. Self-checking migration: fail loudly rather than silently pick one value with max() if
        -- any source_url disagrees with itself across its own chunks on any of the four columns
        -- being collapsed into one row per source.
        --
        -- count(DISTINCT ...) alone is NULL-blind: Postgres's DISTINCT aggregate never counts a
        -- NULL as a value at all, so a source_url whose chunks split between NULL and one real
        -- value on a nullable column (resolved_url, page_last_updated) reads as
        -- count(DISTINCT ...) = 1 -- "consistent" -- and would then be silently collapsed by
        -- max(), which is exactly the silent-max() failure mode this guard exists to rule out. The
        -- second half of each OR below (a NULL present AND a non-NULL present in the same group)
        -- catches that case; fetched_at/last_verified_at are NOT NULL on `documents` already, so
        -- they can never mix with a NULL and need no such second half.
        SELECT source_url INTO inconsistent_source
        FROM documents
        GROUP BY source_url
        HAVING count(DISTINCT resolved_url) > 1
            OR (count(*) FILTER (WHERE resolved_url IS NULL) > 0
                AND count(*) FILTER (WHERE resolved_url IS NOT NULL) > 0)
            OR count(DISTINCT page_last_updated) > 1
            OR (count(*) FILTER (WHERE page_last_updated IS NULL) > 0
                AND count(*) FILTER (WHERE page_last_updated IS NOT NULL) > 0)
            OR count(DISTINCT fetched_at) > 1
            OR count(DISTINCT last_verified_at) > 1
        LIMIT 1;

        IF FOUND THEN
            RAISE EXCEPTION
                'documents.source_url=% has more than one distinct value of resolved_url, '
                'page_last_updated, fetched_at, or last_verified_at across its own chunks -- cannot '
                'safely collapse to one row per source. Refusing to guess with max(); resolve the '
                'inconsistency in `documents` before re-running this migration.',
                inconsistent_source.source_url;
        END IF;

        -- ii. Backfill `sources`, one row per distinct source_url currently in `documents`. Values
        -- carried across unchanged; last_success_at/last_changed_at/change_count/status are
        -- initialized as if this source's most recent (and only known) crawl was a success that
        -- happened to also be the one that produced its current content.
        INSERT INTO sources (
            source_url, resolved_url, page_last_updated, fetched_at, last_verified_at,
            last_changed_at, last_success_at, change_count, status
        )
        SELECT
            source_url,
            max(resolved_url),
            max(page_last_updated),
            max(fetched_at),
            max(last_verified_at),
            max(fetched_at),       -- last_changed_at := fetched_at
            max(last_verified_at), -- last_success_at := last_verified_at
            0,                     -- change_count
            'ok'                   -- status
        FROM documents
        GROUP BY source_url
        ON CONFLICT (source_url) DO NOTHING;

        -- iii. Add the FK only if it does not already exist, so re-running this block (should the
        -- legacy columns somehow still be present) never errors on a duplicate constraint.
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'documents_source_url_fkey'
        ) THEN
            ALTER TABLE documents
                ADD CONSTRAINT documents_source_url_fkey
                FOREIGN KEY (source_url) REFERENCES sources (source_url)
                ON DELETE RESTRICT ON UPDATE CASCADE;
        END IF;

        -- iv. Only now, with every source backfilled into `sources` and the FK in place, drop the
        -- columns that moved there.
        ALTER TABLE documents
            DROP COLUMN resolved_url,
            DROP COLUMN page_last_updated,
            DROP COLUMN fetched_at,
            DROP COLUMN last_verified_at;
    END IF;
END $$;

-- Phase 8 round 3: semantic response cache (app/cache.py). Off by default
-- (Settings.SEMANTIC_CACHE_ENABLED=False, see app/config.py) -- this table exists on every fresh
-- clone whether or not the setting is on, but nothing reads from or writes to it until it is, so
-- `docker compose up` and the CI invariant gate are unaffected either way.
--
-- One row per cached question embedding. `corpus_version` is app/cache.py::corpus_version's
-- output at the moment this row was written; a corpus change (a re-index that moves
-- `sources.last_changed_at`, or changes `documents`' row count) makes every row written under the
-- OLD version both unreachable (app/cache.py::lookup only ever searches the CURRENT version) and,
-- on the next write, actively deleted (app/cache.py::store) -- see that module's own docstring for
-- why both halves matter. `response_type` is constrained to the two cacheable types (see
-- app/schemas.py::ResponseType and app/cache.py::CACHEABLE_RESPONSE_TYPES) as a second,
-- database-level guard against ever caching a CLARIFY/NO_ANSWER/BLOCKED_UNVERIFIED response, not
-- just an application-level convention.
CREATE TABLE IF NOT EXISTS query_cache (
    id                 BIGSERIAL PRIMARY KEY,
    embedding          VECTOR(768) NOT NULL,
    corpus_version     TEXT NOT NULL,
    response_type      TEXT NOT NULL CHECK (response_type IN ('answer', 'refusal_advice')),
    response_json      JSONB NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS query_cache_corpus_version_idx ON query_cache (corpus_version);
CREATE INDEX IF NOT EXISTS query_cache_embedding_hnsw
    ON query_cache USING hnsw (embedding vector_cosine_ops);

-- Phase 8 round 3: the daily generation budget cap (app/usage.py, read by app/pipeline.py before
-- every call to the generator). One row per UTC calendar day, created on that day's first
-- generation call; a day with no row at all has made zero generation calls, not an unknown count.
-- Persisted so a service restart never resets the count mid-day -- see
-- app/usage.py::get_generation_count_today/record_generation_call.
CREATE TABLE IF NOT EXISTS daily_generation_counts (
    day                DATE PRIMARY KEY,
    generation_count   INT NOT NULL DEFAULT 0
);

-- Phase 8 round 3: persistent usage counters (app/usage.py, exposed by GET /usage in
-- app/main.py). `usage_totals` is a single row (id is always 1, enforced by the CHECK) holding a
-- running count of every query handled, ever. `usage_sessions` holds one row per DISTINCT
-- anonymous session hash ever seen (see app/usage.py::hash_session_identifier for how that hash is
-- computed and why it cannot be reversed to identify anyone) -- "distinct_sessions" is a plain
-- count(*) over this table, never re-derived. NEITHER table stores a raw client identifier, a
-- question, or anything else that could be personally identifying -- see ARCHITECTURE.md, "No
-- personal identifying information is stored".
CREATE TABLE IF NOT EXISTS usage_totals (
    id             INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    total_queries  BIGINT NOT NULL DEFAULT 0
);
INSERT INTO usage_totals (id, total_queries) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS usage_sessions (
    session_hash   TEXT PRIMARY KEY,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
