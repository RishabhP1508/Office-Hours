"""Postgres access: a connection pool and the retrieval query.

Phase 0 retrieval is a plain cosine nearest-neighbor scan (`ORDER BY embedding <=> $1 LIMIT k`) with
no index, over the whole `documents` table. Hybrid RRF with a tsvector column and an HNSW index
arrives in Phase 3 (see ARCHITECTURE.md, "Phase 0 uses a sequential cosine scan with no index").
"""

from dataclasses import dataclass
from datetime import date, datetime

from pgvector import Vector
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


@dataclass
class RetrievedChunk:
    id: int
    content: str
    source_url: str
    resolved_url: str | None
    section_heading: str
    heading_level: int
    page_last_updated: date | None
    fetched_at: datetime
    last_verified_at: datetime
    distance: float


async def _configure(conn) -> None:
    await register_vector_async(conn)


def make_pool(database_url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(database_url, open=False, configure=_configure)


async def search(
    pool: AsyncConnectionPool, query_embedding: list[float], k: int
) -> list[RetrievedChunk]:
    """Return the k nearest chunks to query_embedding by cosine distance, closest first."""
    vector = Vector(query_embedding)
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, content, source_url, resolved_url, section_heading, heading_level,
                       page_last_updated, fetched_at, last_verified_at,
                       embedding <=> %(embedding)s AS distance
                FROM documents
                ORDER BY embedding <=> %(embedding)s
                LIMIT %(k)s
                """,
                {"embedding": vector, "k": k},
            )
            rows = await cur.fetchall()
    return [RetrievedChunk(**row) for row in rows]
