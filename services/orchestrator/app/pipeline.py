"""The query pipeline.

classify (clarify, then advice-vs-information) -> retrieve (hybrid RRF, Phase 3 -- see
app/db.py::hybrid_search) -> no-answer check -> generate -> verify citations -> render. This is the
full Phase 4 pipeline; freshness (volatile-topic flagging, Phase 5) is the only seam still open:

    classify -> clarify -> retrieve -> generate -> verify -> freshness

Concretely, in order:

1. Clarify: if the query is too vague to retrieve against at all (app/guardrails/clarifier.py),
   return CLARIFY immediately -- one question, empty citations, empty contexts, no embedding call,
   no database query.
2. Classify: advice vs. information (app/guardrails/classifier.py). An advice verdict does NOT skip
   retrieval or return a canned template -- see step 3 onward and docs/adr/0002-advice-vs-
   information-line.md for why.
3. Retrieve: hybrid RRF, unchanged from Phase 3, regardless of the classification.
4. No-answer check: if retrieval returned nothing, or the MINIMUM cosine distance across every
   retrieved chunk exceeds Settings.NO_ANSWER_MAX_DISTANCE, return NO_ANSWER and never call the
   generator at all. This is the minimum across all retrieved chunks, not the RRF-top-1 chunk's own
   distance -- RRF-top-1 is a fused-rank quantity, not a semantic-closeness one, and can be noisier
   than the single closest chunk actually retrieved (see Settings.NO_ANSWER_MAX_DISTANCE's comment).
5. Generate: SYSTEM_PROMPT for information, REFUSAL_SYSTEM_PROMPT for advice (app/prompts.py) --
   both generate from the same retrieved context, with bracket citations.
6. Strip a trailing source-list block if the model appended one despite being told not to
   (app/prompts.py::strip_source_list_block) -- BEFORE verification, not after (see the comment at
   the call site for why the order matters).
7. Verify citations (app/guardrails/citations.py): block the generated text and return
   BLOCKED_UNVERIFIED if a cited index falls outside the retrieved range, or if an ANSWER carries no
   citation at all. Then, only if verification passed, append the DSO/attorney redirect sentence to
   an advice response if the model did not already include one.
"""

import re
from datetime import UTC, datetime

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.db import RetrievedChunk, hybrid_search
from app.guardrails.citations import verify_citations
from app.guardrails.clarifier import CLARIFY_QUESTION, is_too_vague
from app.guardrails.classifier import classify_advice
from app.prompts import (
    REFUSAL_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_user_prompt,
    strip_source_list_block,
)
from app.providers.embeddings import Embedder
from app.providers.llm import LLM
from app.schemas import AnswerResponse, Citation, ResponseType, RetrievedContext
from app.telemetry import get_tracer

_NO_ANSWER_MESSAGE = (
    "I don't see this covered in my sources, so I'm not going to guess at an answer. Try "
    "rephrasing the question, or check with your DSO or a licensed immigration attorney."
)

_BLOCKED_MESSAGE = (
    "I generated an answer to this, but it did not pass this tool's citation check, so I'm not "
    "showing it. Please try rephrasing the question."
)

_DSO_REDIRECT_SENTENCE = (
    "For advice on your own situation, talk to your DSO or a licensed immigration attorney."
)

# Whether a generated answer already contains some form of the DSO/attorney redirect, so
# _DSO_REDIRECT_SENTENCE is never appended twice. Checked with a word-boundary regex (not a plain
# substring test) so it does not, for example, match "dso" inside an unrelated longer word; \b on
# "dso" alone is enough since it is not a common English substring of anything else in this corpus.
_REDIRECT_RE = re.compile(
    r"\bdso\b|designated school official|licensed immigration attorney", re.IGNORECASE
)


def _has_redirect(text: str) -> bool:
    return bool(_REDIRECT_RE.search(text))


def _citation_url(chunk: RetrievedChunk) -> str:
    # Citations point at the resolved URL, not the manifest URL, so a redirected source does not
    # send a reader through a redirect to a page whose title no longer matches the citation.
    return chunk.resolved_url or chunk.source_url


def _snippet(content: str, max_len: int = 240) -> str:
    text = " ".join(content.split())
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "..."


def _empty_response(
    *, answer: str, response_type: ResponseType, refusal_reason: str | None
) -> AnswerResponse:
    return AnswerResponse(
        answer=answer,
        citations=[],
        contexts=[],
        response_type=response_type.value,
        refusal_reason=refusal_reason,
        generated_at=datetime.now(UTC),
    )


async def answer_question(
    question: str,
    *,
    pool: AsyncConnectionPool,
    embedder: Embedder,
    llm: LLM,
    settings: Settings,
) -> AnswerResponse:
    tracer = get_tracer()

    # --- Step 1: clarify. Must not touch pool or embedder at all if it fires. ---
    with tracer.start_as_current_span("classify") as classify_span:
        vague = is_too_vague(question, min_content_words=settings.CLARIFY_MIN_CONTENT_WORDS)
        classify_span.set_attribute("clarify_triggered", vague)
        if vague:
            classify_span.set_attribute("response_type", ResponseType.CLARIFY.value)
            return _empty_response(
                answer=CLARIFY_QUESTION,
                response_type=ResponseType.CLARIFY,
                refusal_reason="query_too_vague",
            )

        # --- Step 2: classify advice vs. information. Sees only the question text. ---
        classification = await classify_advice(question, llm=llm, settings=settings)
        classify_span.set_attribute("advice_decided_by", classification.decided_by)
        classify_span.set_attribute("is_advice", classification.is_advice)

    # --- Step 3: retrieve. Runs regardless of the classification -- an advice-seeking question
    # --- still needs the same retrieved context an informational one would get (see module
    # --- docstring and docs/adr/0002-advice-vs-information-line.md). ---
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
        top1_distance = chunks[0].distance if chunks else None
        # The no-answer gate asks "is anything I am about to hand the generator actually
        # relevant", so it gates on the MINIMUM distance across every retrieved chunk, not on
        # chunks[0] (the RRF-top-1 chunk). RRF-top-1 is a fused-rank quantity that can disagree
        # with which chunk is actually closest: for "How much does one H-1B registration cost?"
        # the RRF-top-1 chunk sits at distance 0.3311 while the closest retrieved chunk is 0.1926.
        # top1_distance is still recorded on the span for visibility, but min_distance is what the
        # gate below compares against the threshold.
        min_distance = min((c.distance for c in chunks), default=None)
        # -1.0 is a sentinel meaning "no chunks at all", not a real distance (cosine distance is
        # always >= 0); OTel span attributes cannot be None.
        retrieve_span.set_attribute(
            "top1_distance", top1_distance if top1_distance is not None else -1.0
        )
        retrieve_span.set_attribute(
            "min_distance", min_distance if min_distance is not None else -1.0
        )

        # --- Step 4: no-answer check. Never calls the generator if this fires. ---
        no_relevant_chunk = not chunks or (
            min_distance is not None and min_distance > settings.NO_ANSWER_MAX_DISTANCE
        )
        if no_relevant_chunk:
            retrieve_span.set_attribute("response_type", ResponseType.NO_ANSWER.value)
            reason = "no_chunks_retrieved" if not chunks else "min_distance_exceeds_threshold"
            return _empty_response(
                answer=_NO_ANSWER_MESSAGE,
                response_type=ResponseType.NO_ANSWER,
                refusal_reason=reason,
            )

    context = [
        {
            "content": chunk.content,
            "citation_url": _citation_url(chunk),
        }
        for chunk in chunks
    ]
    user_prompt = build_user_prompt(question, context)

    # --- Step 5: generate, with the refusal-shaped prompt for an advice verdict. ---
    system_prompt = REFUSAL_SYSTEM_PROMPT if classification.is_advice else SYSTEM_PROMPT
    candidate_response_type = (
        ResponseType.REFUSAL_ADVICE if classification.is_advice else ResponseType.ANSWER
    )
    with tracer.start_as_current_span("generate") as generate_span:
        generate_span.set_attribute("model", settings.LLM_MODEL)
        generate_span.set_attribute("prompt_chars", len(user_prompt))
        generate_span.set_attribute("candidate_response_type", candidate_response_type.value)
        try:
            answer_text = await llm.generate(system_prompt, user_prompt)
        except Exception as exc:  # noqa: BLE001 - re-raised after recording on the span
            generate_span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        generate_span.set_attribute("answer_chars", len(answer_text))

    # --- Step 6: strip a trailing source-list block BEFORE verification, not after. ---
    # Verification has to check the text that will actually render. If citations were verified on
    # the raw generated text and the source-list block stripped afterward, a model that put every
    # one of its brackets only inside a trailing "Source URLs:"-style block (and none inline) would
    # pass verification against the raw text, then have that block stripped, and render with zero
    # visible bracket citations despite having "passed". Stripping first closes that gap: the
    # citation check below inspects exactly the string that ends up in the response.
    answer_text = strip_source_list_block(answer_text)

    # --- Step 7: verify citations. Blocks rendering the generated text on failure. ---
    verification = verify_citations(
        answer_text, num_contexts=len(chunks), response_type=candidate_response_type.value
    )

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

    if not verification.ok:
        trace.get_current_span().set_attribute(
            "response_type", ResponseType.BLOCKED_UNVERIFIED.value
        )
        return AnswerResponse(
            answer=_BLOCKED_MESSAGE,
            citations=citations,
            contexts=contexts,
            response_type=ResponseType.BLOCKED_UNVERIFIED.value,
            refusal_reason=verification.reason,
            generated_at=datetime.now(UTC),
        )

    # Append the DSO/attorney redirect to an advice response if the model did not already include
    # one. This runs AFTER verification, but cannot affect verification's outcome either way: the
    # redirect sentence never contains a bracket, so appending it can never change which indices
    # verify_citations saw above.
    refusal_reason = None
    if candidate_response_type == ResponseType.REFUSAL_ADVICE:
        refusal_reason = "query_asks_for_personal_advice"
        if not _has_redirect(answer_text):
            answer_text = f"{answer_text}\n\n{_DSO_REDIRECT_SENTENCE}"

    trace.get_current_span().set_attribute("response_type", candidate_response_type.value)

    return AnswerResponse(
        answer=answer_text,
        citations=citations,
        contexts=contexts,
        response_type=candidate_response_type.value,
        refusal_reason=refusal_reason,
        generated_at=datetime.now(UTC),
    )
