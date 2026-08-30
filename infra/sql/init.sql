-- Office Hours: document store for retrieval.
-- One Postgres instance holds the chunks, the embeddings, and (from Phase 3) the keyword index.
-- No dedicated vector database: see ARCHITECTURE.md "PostgreSQL with pgvector, no dedicated vector database".

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id                 BIGSERIAL PRIMARY KEY,
    content            TEXT NOT NULL,
    source_url         TEXT NOT NULL,
    resolved_url       TEXT,
    section_heading    TEXT NOT NULL,
    heading_level      INT NOT NULL,
    page_last_updated  DATE,
    fetched_at         TIMESTAMPTZ NOT NULL,
    last_verified_at   TIMESTAMPTZ NOT NULL,
    embedding          VECTOR(768) NOT NULL
);

COMMENT ON TABLE documents IS
    'fetched_at and last_verified_at are deliberately separate columns: fetched_at is when the '
    'content was last downloaded, last_verified_at is when it was last checked, changed or not. '
    'A re-crawl that finds a page unchanged updates last_verified_at only, so a page checked this '
    'morning and found unchanged does not read as stale.';

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
