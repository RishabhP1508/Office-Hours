"""FastAPI app: lifespan-managed DB pool, /health, and POST /query."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.db import make_pool
from app.pipeline import answer_question
from app.providers.embeddings import get_embedder
from app.providers.llm import get_llm
from app.schemas import AnswerResponse, QueryRequest
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
setup_telemetry(get_settings(), app)


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
