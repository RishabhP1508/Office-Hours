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

-- Phase 0 uses a sequential cosine scan (ORDER BY embedding <=> $1 LIMIT k) with no index: exact
-- search over a few hundred rows is fast and gives an exact ground truth for retrieval quality.
-- The HNSW index (vector_cosine_ops) and the tsvector keyword column arrive in Phase 3.
