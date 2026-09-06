"""FastAPI app: lifespan-managed DB pool, /health, POST /query, POST /query/stream, and
GET /sources/status.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse

from app.config import get_settings
from app.db import make_pool
from app.guardrails.freshness import source_health_state, sources_freshness_state
from app.pipeline import answer_question
from app.providers.embeddings import get_embedder
from app.providers.llm import get_llm
from app.schemas import AnswerResponse, BrokenSource, QueryRequest, SourcesStatus
from app.telemetry import setup_telemetry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = make_pool(settings.DATABASE_URL)
    await pool.open()
    app.state.settings = settings
    app.state.pool = pool
    app.state.embedder = get_embedder(settings)
    app.state.llm = get_llm(settings)
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="Office Hours Orchestrator", lifespan=lifespan)
_module_settings = get_settings()
setup_telemetry(_module_settings, app)

# The browser (services/frontend) calls this orchestrator directly -- there is no gateway in front
# of it until Phase 7 -- so it has to answer the browser's CORS preflight itself. Origins come from
# Settings.ALLOWED_ORIGINS (comma-separated), never hardcoded.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip() for origin in _module_settings.ALLOWED_ORIGINS.split(",") if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/query", response_model=AnswerResponse)
async def query(request: QueryRequest) -> AnswerResponse:
    try:
        return await answer_question(
            request.question,
            pool=app.state.pool,
            embedder=app.state.embedder,
            llm=app.state.llm,
            settings=app.state.settings,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a 502 rather than a bare 500 traceback
        # Log the full traceback before converting to a 502: without this, the orchestrator log
        # shows only "502 Bad Gateway" with no indication of what actually failed underneath
        # (e.g. Ollama itself returning a non-200 -- context overflow, OOM, a model unload
        # mid-request). The exception class name also goes into the client-facing detail string
        # so a caller (eval/run.py in particular) can see what failed without needing the server
        # log.
        logger.exception("POST /query failed for question=%r", request.question)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to answer question ({type(exc).__name__}): {exc}",
        ) from exc


@app.post("/query/stream")
async def query_stream(request: QueryRequest) -> StreamingResponse:
    """Same request body as POST /query, but streamed over Server-Sent Events so the frontend can
    show real pipeline progress instead of one long silent wait (a query can take 20-140s against
    the local generator). Every message is framed `data: <json>\\n\\n`; the terminal message is
    either `{"event": "result", "response": <AnswerResponse>}` or, on an unhandled exception,
    `{"event": "error", "detail": "<type>: <msg>"}` -- logged the same way POST /query's own
    exception handler logs one, so an error here is just as diagnosable from the server log.

    Driven by an asyncio.Queue: `answer_question` runs as a background task and puts one dict onto
    the queue per stage event (via `on_event`) plus one final result/error dict, and this generator
    drains the queue and yields each one as it arrives -- real progress, not a client-side guess at
    how long each stage takes.
    """
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def on_event(event: dict) -> None:
        await queue.put(event)

    async def run_pipeline() -> None:
        try:
            response = await answer_question(
                request.question,
                pool=app.state.pool,
                embedder=app.state.embedder,
                llm=app.state.llm,
                settings=app.state.settings,
                on_event=on_event,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as an "error" SSE message, not a crash
            logger.exception("POST /query/stream failed for question=%r", request.question)
            await queue.put({"event": "error", "detail": f"{type(exc).__name__}: {exc}"})
        else:
            await queue.put({"event": "result", "response": response.model_dump(mode="json")})
        finally:
            await queue.put(None)  # sentinel: no more events

    async def event_stream() -> AsyncIterator[str]:
        # Sent before the pipeline task even starts, so the client has a first byte immediately
        # rather than waiting on classify/retrieve/generate/verify to produce one.
        yield f"data: {json.dumps({'event': 'ping'})}\n\n"
        task = asyncio.create_task(run_pipeline())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            await task

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/sources/status", response_model=SourcesStatus)
async def sources_status() -> SourcesStatus:
    """The header's "live sources" trust indicator reads this. Phase 7: reads `sources` directly (14
    rows, one per manifest entry) instead of aggregating over `documents` (221 chunks) -- the
    per-source crawl bookkeeping that used to be duplicated on every chunk now lives on exactly one
    row per source, so there is nothing left to GROUP BY. Never a hardcoded or cached claim, and
    never a plain `max(last_verified_at)`, which would let one freshly re-crawled source claim
    "checked today" while the rest of the corpus sat stale (see app/schemas.py::SourcesStatus).

    `min`/`max(last_verified_at)` across all `sources` rows are the weakest/freshest link;
    `sources_freshness_state` (app/guardrails/freshness.py) turns the weakest link's age into the
    "current"/"recent"/"stale"/"unknown" band the frontend renders. `source_health_state` (same
    module) makes the SEPARATE broken-vs-ok call per source -- never duplicated here as SQL -- and
    every broken row is reported in full via `BrokenSource` so the frontend can say something
    concrete without recomputing the rule itself.
    """
    now = datetime.now(UTC)
    async with app.state.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT count(*), min(last_verified_at), max(last_verified_at),
                       count(*) FILTER (WHERE last_verified_at < now() - interval '24 hours')
                FROM sources
                """)
            source_count, oldest_verified_at, newest_verified_at, stale_source_count = (
                await cur.fetchone()
            )
            await cur.execute("""
                SELECT source_url, status, consecutive_failures, last_error, last_http_status,
                       last_success_at
                FROM sources
                """)
            rows = await cur.fetchall()

    freshness_state, age_hours = sources_freshness_state(oldest_verified_at, now)

    broken_sources = []
    for (
        source_url,
        status,
        consecutive_failures,
        last_error,
        last_http_status,
        last_success_at,
    ) in rows:
        health = source_health_state(
            consecutive_failures=consecutive_failures,
            status=status,
            last_success_at=last_success_at,
            now=now,
            broken_after_failures=app.state.settings.SOURCE_BROKEN_CONSECUTIVE_FAILURES,
            broken_after_no_success_days=app.state.settings.SOURCE_BROKEN_NO_SUCCESS_DAYS,
        )
        if health == "broken":
            broken_sources.append(
                BrokenSource(
                    source_url=source_url,
                    status=status,
                    consecutive_failures=consecutive_failures,
                    last_error=last_error,
                    last_http_status=last_http_status,
                    last_success_at=last_success_at,
                )
            )

    return SourcesStatus(
        as_of=now.date(),
        source_count=source_count,
        oldest_verified_at=oldest_verified_at,
        newest_verified_at=newest_verified_at,
        stale_source_count=stale_source_count,
        age_hours=age_hours,
        freshness_state=freshness_state,
        broken_source_count=len(broken_sources),
        broken_sources=broken_sources,
    )
