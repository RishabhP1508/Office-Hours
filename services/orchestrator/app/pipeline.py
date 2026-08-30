"""The query pipeline.

embed -> retrieve (hybrid RRF, Phase 3 -- see app/db.py::hybrid_search) -> generate -> build
citations. This is deliberately the whole pipeline for now. The seams for the guardrail stages
arrive in later phases and are marked below:

    classify -> clarify -> retrieve -> generate -> verify -> freshness

Phase 0 only implements retrieve and generate. classify/clarify (Phase 4, advice-vs-information and
one clarifying question for a vague query) sit before retrieve. verify (Phase 4, programmatic
citation checking) and freshness (Phase 4/5, volatile-topic flagging) sit after generate.
"""

from datetime import UTC, datetime

from opentelemetry.trace import Status, StatusCode
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.db import RetrievedChunk, hybrid_search
from app.prompts import SYSTEM_PROMPT, build_user_prompt
from app.providers.embeddings import Embedder
from app.providers.llm import LLM
from app.schemas import AnswerResponse, Citation, RetrievedContext
from app.telemetry import get_tracer


def _citation_url(chunk: RetrievedChunk) -> str:
    # Citations point at the resolved URL, not the manifest URL, so a redirected source does not
    # send a reader through a redirect to a page whose title no longer matches the citation.
    return chunk.resolved_url or chunk.source_url


def _snippet(content: str, max_len: int = 240) -> str:
    text = " ".join(content.split())
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "..."


async def answer_question(
    question: str,
    *,
    pool: AsyncConnectionPool,
    embedder: Embedder,
    llm: LLM,
    settings: Settings,
) -> AnswerResponse:
    tracer = get_tracer()

    # classify / clarify seam (Phase 4): every question is treated as informational in Phase 0.

    with tracer.start_as_current_span("retrieve") as retrieve_span:
        retrieve_span.set_attribute("top_k", settings.RETRIEVAL_TOP_K)
        retrieve_span.set_attribute("retrieval_mode", "hybrid_rrf")
        retrieve_span.set_attribute("rrf_k", settings.RRF_K)
        retrieve_span.set_attribute("candidate_pool", settings.HYBRID_CANDIDATE_POOL)
        [query_embedding] = await embedder.embed([question])
        chunks = await hybrid_search(
            pool,
            query_embedding,
            question,
            settings.RETRIEVAL_TOP_K,
            rrf_k=settings.RRF_K,
            candidate_pool=settings.HYBRID_CANDIDATE_POOL,
        )
        retrieve_span.set_attribute("result_count", len(chunks))
        retrieve_span.set_attribute(
            "semantic_only_hits",
            sum(1 for c in chunks if c.semantic_rank is not None and c.keyword_rank is None),
        )
        retrieve_span.set_attribute(
            "keyword_only_hits",
            sum(1 for c in chunks if c.keyword_rank is not None and c.semantic_rank is None),
        )
        retrieve_span.set_attribute(
            "both_arms_hits",
            sum(1 for c in chunks if c.semantic_rank is not None and c.keyword_rank is not None),
        )

    context = [
        {
            "content": chunk.content,
            "citation_url": _citation_url(chunk),
        }
        for chunk in chunks
    ]
    user_prompt = build_user_prompt(question, context)

    with tracer.start_as_current_span("generate") as generate_span:
        generate_span.set_attribute("model", settings.LLM_MODEL)
        generate_span.set_attribute("prompt_chars", len(user_prompt))
        try:
            answer_text = await llm.generate(SYSTEM_PROMPT, user_prompt)
        except Exception as exc:  # noqa: BLE001 - re-raised after recording on the span
            generate_span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        generate_span.set_attribute("answer_chars", len(answer_text))

    # --- verify / freshness: Phase 4/5 seam. Phase 0 renders every generated answer as-is. ---

    # citations and contexts are both derived from the same `chunks` list, in the same order, so
    # they cannot drift apart: position i (1-based) in `contexts` is exactly the [i] the model was
    # shown in the prompt and may cite, and citations[i - 1] is the same chunk.
    citations = [
        Citation(
            source_url=_citation_url(chunk), chunk_id=chunk.id, snippet=_snippet(chunk.content)
        )
        for chunk in chunks
    ]
    contexts = [
        RetrievedContext(
            chunk_id=chunk.id,
            source_url=_citation_url(chunk),
            section_heading=chunk.section_heading,
            content=chunk.content,
        )
        for chunk in chunks
    ]

    return AnswerResponse(
        answer=answer_text,
        citations=citations,
        contexts=contexts,
        generated_at=datetime.now(UTC),
    )
