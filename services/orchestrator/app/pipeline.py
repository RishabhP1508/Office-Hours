"""The query pipeline.

classify (clarify, then advice-vs-information) -> retrieve (hybrid RRF, Phase 3 -- see
app/db.py::hybrid_search) -> no-answer check -> generate -> verify citations -> freshness -> render.
This is the full Phase 5 pipeline:

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
6. Normalize the model's own native citation markup ("【N†...】") to this project's "[N]" bracket
   convention (app/prompts.py::normalize_native_citation_markup, Phase 8 round 4), then strip a
   trailing source-list block if the model appended one despite being told not to
   (app/prompts.py::strip_source_list_block) -- BOTH BEFORE verification, not after (see the
   comment at the call site for why the order matters).
7. Verify citations (app/guardrails/citations.py): block the generated text and return
   BLOCKED_UNVERIFIED if a cited index falls outside the retrieved range, or if an ANSWER carries no
   citation at all. Then, only if verification passed, append the DSO/attorney redirect sentence to
   an advice response if the model did not already include one.
8. Freshness (app/guardrails/freshness.py): build the structured freshness block from the same
   retrieved chunks and, for ANSWER and REFUSAL_ADVICE only, append the effective-date notice
   sentence to the answer text -- but only for a dated source that is either the top-ranked
   retrieved chunk or actually cited in the generated text (see build_freshness's own docstring for
   why both conditions are needed); a dated source retrieved incidentally, neither top-ranked nor
   cited, stays visible in the structured `Freshness.sources` field but appends no text. CLARIFY,
   NO_ANSWER, and BLOCKED_UNVERIFIED responses carry freshness=None and no appended text -- none of
   those three renders a generated answer at all.

Phase 6 addition: an optional, keyword-only `on_event` callback (app/main.py's POST /query/stream
uses it to drive the frontend's progress UI; POST /query passes nothing, so its behavior is
byte-identical to before this callback existed). When given, it is awaited at the four real stage
boundaries above -- classify, retrieve, generate, verify -- with a `{"event": "stage", "stage":
..., "status": "start"|"done", ...}` dict, and ONLY for a stage this call actually reaches: a
CLARIFY response emits classify start/done and returns; a NO_ANSWER response emits classify and
retrieve start/done and returns; neither ever emits a generate or verify event, because neither
stage ran. `on_event=None` (the default) makes every one of these a no-op, so nothing about the
pipeline's own control flow or return value changes when it is omitted.

Phase 8 round 3 additions, both off by default (Settings.SEMANTIC_CACHE_ENABLED=False,
Settings.DAILY_GENERATION_CAP=0) so a fresh clone's behavior, and every stage event sequence above,
is byte-for-byte unaffected unless an operator turns one on:

3.5. Semantic cache lookup (app/cache.py), inside the retrieve span, using the SAME query embedding
     retrieval itself needs -- never a second embed call. Passed `is_advice=classification.
     is_advice` from step 2's already-computed classification (Phase 8 round 4 -- lookup()
     REQUIRES this keyword argument; see app/cache.py's module docstring for the bug this closes:
     a similarity threshold alone cannot tell an "informational" question from an "advice-seeking"
     one about the same topic, so a cache hit is now only ever a hit if the stored response was
     ALSO cached under this same classification). A hit returns that cached ANSWER/REFUSAL_ADVICE
     response immediately (emitting retrieve's own "done" event, with source_count read from the
     cached response's own citations, then never emitting generate or verify at all -- the same
     "early return after retrieve" shape NO_ANSWER already has). A miss (or the cache being
     disabled) falls through to hybrid_search exactly as before.
4.5. Daily generation budget cap (app/usage.py), reached only once the no-answer check above has
     already passed (there is something relevant to point at). If today's UTC count of generation
     calls has reached Settings.DAILY_GENERATION_CAP, the generator is never called: this returns a
     degraded response (reusing ResponseType.NO_ANSWER with refusal_reason=
     "daily_generation_cap_reached" -- see _BUDGET_EXCEEDED_MESSAGE's own comment for why no sixth
     response type was added) that carries the real citations/contexts retrieval already found,
     without generating a summary of them. A successful generate() call (step 5) increments that
     count afterward.

Phase 8 round 4 addition, also off by default (app/langfuse_telemetry.py, Settings.
LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY empty): immediately after a successful generate() call
(step 5), record_generation_trace records one Langfuse generation carrying the prompt text's own
content-hash version (app/prompts.py::SYSTEM_PROMPT_VERSION / REFUSAL_SYSTEM_PROMPT_VERSION), the
model that actually served the call, and its REAL reported token usage (app/providers/llm.py::
LLM.last_usage) -- never estimated, never able to affect the response either way (see that
module's own docstring for why it can never raise into this function).
"""

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from psycopg_pool import AsyncConnectionPool

from app import cache, usage
from app.config import Settings
from app.db import RetrievedChunk, hybrid_search
from app.guardrails.citations import parse_cited_indices, verify_citations
from app.guardrails.clarifier import CLARIFY_QUESTION, is_too_vague
from app.guardrails.classifier import classify_advice
from app.guardrails.freshness import build_freshness, freshness_notice_text
from app.langfuse_telemetry import record_generation_trace
from app.prompts import (
    REFUSAL_SYSTEM_PROMPT,
    REFUSAL_SYSTEM_PROMPT_VERSION,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_VERSION,
    build_user_prompt,
    normalize_native_citation_markup,
    strip_source_list_block,
)
from app.providers.embeddings import Embedder
from app.providers.llm import LLM
from app.schemas import AnswerResponse, Citation, ResponseType, RetrievedContext
from app.telemetry import (
    get_tracer,
    record_generation_call_metric,
    record_generation_tokens_metric,
)

_NO_ANSWER_MESSAGE = (
    "I don't see this covered in my sources, so I'm not going to guess at an answer. Try "
    "rephrasing the question, or check with your DSO or a licensed immigration attorney."
)

_BLOCKED_MESSAGE = (
    "I generated an answer to this, but it did not pass this tool's citation check, so I'm not "
    "showing it. Please try rephrasing the question."
)

# Phase 8 round 3: the daily generation budget cap's degraded response (Settings.DAILY_GENERATION_
# CAP, app/usage.py::budget_exceeded). Reuses ResponseType.NO_ANSWER rather than adding a sixth
# response type -- see this module's docstring, "Step 4.5", for why: adding a new enum member would
# require eval/run.py's closed response_type -> refusal mapping to learn about it too, and this
# project's scope for that file is read-only. `refusal_reason` ("daily_generation_cap_reached")
# is what actually distinguishes this from a real "sources don't cover it" NO_ANSWER for anything
# that inspects the reason rather than just the coarse type. UNLIKE the ordinary NO_ANSWER path,
# this response DOES carry real citations/contexts -- retrieval already found something relevant
# (the no-answer gate already passed by the time this can fire) -- so pointing at those sources is
# both possible and required ("point to the sources, WITHOUT fabricating an answer").
_BUDGET_EXCEEDED_MESSAGE = (
    "Office Hours has reached its daily limit on generating new answers. To avoid guessing, I'm "
    "not generating a summary for this question right now. The sources below are what retrieval "
    "found relevant to your question -- you can check them directly, or try again after the daily "
    "limit resets at midnight UTC."
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
    on_event: Callable[[dict], Awaitable[None]] | None = None,
    classifier_llm: LLM | None = None,
) -> AnswerResponse:
    tracer = get_tracer()

    async def _emit(event: dict) -> None:
        # No-op when on_event is None (the default, and what POST /query passes) -- see this
        # module's docstring. Every call site below awaits this unconditionally so the emission
        # points read the same regardless of whether a caller is listening.
        if on_event is not None:
            await on_event(event)

    # --- Step 1: clarify. Must not touch pool or embedder at all if it fires. ---
    await _emit({"event": "stage", "stage": "classify", "status": "start"})
    with tracer.start_as_current_span("classify") as classify_span:
        vague = is_too_vague(question, min_content_words=settings.CLARIFY_MIN_CONTENT_WORDS)
        classify_span.set_attribute("clarify_triggered", vague)
        if vague:
            classify_span.set_attribute("response_type", ResponseType.CLARIFY.value)
            await _emit({"event": "stage", "stage": "classify", "status": "done"})
            return _empty_response(
                answer=CLARIFY_QUESTION,
                response_type=ResponseType.CLARIFY,
                refusal_reason="query_too_vague",
            )

        # --- Step 2: classify advice vs. information. Sees only the question text. Routed to
        # --- `classifier_llm` when the caller provided one (app/main.py builds this once at
        # --- startup from Settings.CLASSIFIER_LLM_PROVIDER/MODEL -- see
        # --- app/providers/llm.py::get_classifier_llm); `classifier_llm=None` (every existing call
        # --- site, and the default whenever CLASSIFIER_LLM_PROVIDER is unset) falls back to the
        # --- same generator `llm` exactly as before this parameter existed.
        # --- classify_advice's own signature and logic are UNCHANGED by this -- it is simply
        # --- handed a different LLM object.
        classification = await classify_advice(
            question, llm=(classifier_llm or llm), settings=settings
        )
        classify_span.set_attribute("advice_decided_by", classification.decided_by)
        classify_span.set_attribute("is_advice", classification.is_advice)
    await _emit({"event": "stage", "stage": "classify", "status": "done"})

    # --- Step 3: retrieve. Runs regardless of the classification -- an advice-seeking question
    # --- still needs the same retrieved context an informational one would get (see module
    # --- docstring and docs/adr/0002-advice-vs-information-line.md). ---
    await _emit({"event": "stage", "stage": "retrieve", "status": "start"})
    with tracer.start_as_current_span("retrieve") as retrieve_span:
        retrieve_span.set_attribute("top_k", settings.RETRIEVAL_TOP_K)
        retrieve_span.set_attribute("retrieval_mode", "hybrid_rrf")
        retrieve_span.set_attribute("rrf_k", settings.RRF_K)
        retrieve_span.set_attribute("candidate_pool", settings.HYBRID_CANDIDATE_POOL)
        [query_embedding] = await embedder.embed([question])

        # --- Step 3.5 (Phase 8 round 3): semantic cache lookup, using the SAME embedding just
        # --- computed above -- never a second embed call. Off by default
        # --- (Settings.SEMANTIC_CACHE_ENABLED=False); see app/cache.py's module docstring for what
        # --- gets cached, how similarity is measured, and how a corpus change invalidates every
        # --- affected entry. `cache_version` is reused below, at the very end of a successful
        # --- ANSWER/REFUSAL_ADVICE response, to store this exact query under the same corpus
        # --- version it was looked up against.
        #
        # Phase 8 round 5: `cache.corpus_version` returns None (rather than raising) when it cannot
        # even be computed -- see app/cache.py's own docstring, "OPTIONAL INFRASTRUCTURE, NEVER
        # FATAL". `cache_version is None` is treated here as "the cache is unavailable for this
        # request": the lookup below is skipped outright (never called with a None version), and
        # the store call at the very end of this function already only fires when
        # `cache_version is not None`, so both halves of the cache degrade together, cleanly, with
        # no extra error handling needed at this call site.
        cache_version: str | None = None
        if settings.SEMANTIC_CACHE_ENABLED:
            cache_version = await cache.corpus_version(pool)
        if cache_version is not None:
            cached_response = await cache.lookup(
                pool,
                query_embedding,
                is_advice=classification.is_advice,
                threshold=settings.SEMANTIC_CACHE_SIMILARITY_THRESHOLD,
                version=cache_version,
            )
            retrieve_span.set_attribute("cache_hit", cached_response is not None)
            if cached_response is not None:
                retrieve_span.set_attribute("response_type", cached_response.response_type)
                await _emit(
                    {
                        "event": "stage",
                        "stage": "retrieve",
                        "status": "done",
                        "source_count": len(cached_response.citations),
                    }
                )
                # A cache hit is returned VERBATIM, including its original generated_at/freshness --
                # the corpus has not changed since it was written (that is exactly what
                # cache_version gates), so the original, already-verified response is still
                # accurate. No generate or verify event ever fires for this path -- the same
                # "early return after retrieve" shape NO_ANSWER already has (see module docstring).
                return cached_response

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

        # retrieve genuinely completed by this point (chunks fetched, span attributes recorded)
        # whether or not the no-answer gate below ends up firing, so this event fires
        # unconditionally -- a NO_ANSWER response really did retrieve, it just found nothing close
        # enough (see this module's docstring on which stages an early-return path actually
        # reaches).
        await _emit(
            {"event": "stage", "stage": "retrieve", "status": "done", "source_count": len(chunks)}
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

    # citations/contexts depend only on `chunks`, never on the generated answer text, so they are
    # built here -- before step 4.5's budget check and step 5's generate -- rather than after
    # verification the way they used to be. This is a pure reordering with no value change: the
    # degraded budget-exceeded response below needs real citations/contexts to point at, and the
    # ordinary happy path (further down) uses these same two lists exactly as it always did.
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

    # --- Step 4.5 (Phase 8 round 3): daily generation budget cap. Only reachable once the
    # --- no-answer check above has already passed -- there is real, relevant retrieved context to
    # --- point at. Off by default (Settings.DAILY_GENERATION_CAP=0); see
    # --- _BUDGET_EXCEEDED_MESSAGE's own comment for why this reuses ResponseType.NO_ANSWER rather
    # --- than adding a new response type, and app/usage.py::budget_exceeded for the persisted,
    # --- restart-proof counter this reads.
    if settings.DAILY_GENERATION_CAP > 0 and await usage.budget_exceeded(
        pool, settings.DAILY_GENERATION_CAP
    ):
        trace.get_current_span().set_attribute("response_type", ResponseType.NO_ANSWER.value)
        trace.get_current_span().set_attribute("refusal_reason", "daily_generation_cap_reached")
        return AnswerResponse(
            answer=_BUDGET_EXCEEDED_MESSAGE,
            citations=citations,
            contexts=contexts,
            response_type=ResponseType.NO_ANSWER.value,
            refusal_reason="daily_generation_cap_reached",
            generated_at=datetime.now(UTC),
        )

    # --- Step 5: generate, with the refusal-shaped prompt for an advice verdict. ---
    system_prompt = REFUSAL_SYSTEM_PROMPT if classification.is_advice else SYSTEM_PROMPT
    candidate_response_type = (
        ResponseType.REFUSAL_ADVICE if classification.is_advice else ResponseType.ANSWER
    )
    with tracer.start_as_current_span("generate") as generate_span:
        generate_span.set_attribute("model", settings.LLM_MODEL)
        generate_span.set_attribute("prompt_chars", len(user_prompt))
        generate_span.set_attribute("candidate_response_type", candidate_response_type.value)
        await _emit({"event": "stage", "stage": "generate", "status": "start"})
        try:
            answer_text = await llm.generate(system_prompt, user_prompt)
        except Exception as exc:  # noqa: BLE001 - re-raised after recording on the span
            generate_span.set_status(Status(StatusCode.ERROR, str(exc)))
            # No "generate":"done" event on this path -- generation did not actually complete, and
            # this exception propagates out of answer_question entirely (no verify stage runs
            # either), so emitting "done" here would claim a stage finished that did not.
            raise
        generate_span.set_attribute("answer_chars", len(answer_text))
        # Phase 8 round 3: the dashboard's OTel metric (unconditional, mirrors tracing -- see
        # app/telemetry.py) is recorded on every successful generate() call regardless of whether
        # the budget cap feature is configured. The PERSISTED Postgres counter
        # (app/usage.py::record_generation_call), by contrast, is only written when the cap is
        # actually configured (Settings.DAILY_GENERATION_CAP > 0) -- a fresh clone never pays for
        # that database write unless an operator turned the cap feature on. Both are recorded only
        # AFTER generate() succeeds (this line is unreachable on the exception path above), so both
        # reflect generation calls that actually happened, not merely attempted.
        record_generation_call_metric()
        # Phase 8 round 4: the dashboard's real token-count panel (infra/observability/
        # grafana-dashboard.json), replacing the earlier generation-call-count cost proxy -- see
        # app/telemetry.py's own comment. `llm.last_usage` is whichever provider actually served
        # this call's own REAL, provider-reported counts (never estimated); None fields (StubLLM
        # reports none at all) add nothing, never a fabricated number.
        record_generation_tokens_metric(llm.last_usage)
        if settings.DAILY_GENERATION_CAP > 0:
            await usage.record_generation_call(pool)
        # Phase 8 round 4: Langfuse trace (app/langfuse_telemetry.py) -- a silent no-op unless an
        # operator has configured LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY, and never able to affect
        # this response either way (it runs strictly before verify_citations below, and cannot
        # raise into this function -- see that module's own docstring). `prompt_version` is a
        # content hash of the EXACT prompt text just used (app/prompts.py::SYSTEM_PROMPT_VERSION /
        # REFUSAL_SYSTEM_PROMPT_VERSION), and `llm.last_usage` is whichever provider actually
        # served this call's own REAL, reported token counts (never estimated).
        record_generation_trace(
            name="generate",
            model_id=llm.model_id,
            prompt_version=(
                REFUSAL_SYSTEM_PROMPT_VERSION if classification.is_advice else SYSTEM_PROMPT_VERSION
            ),
            response_type=candidate_response_type.value,
            is_advice=classification.is_advice,
            user_prompt=user_prompt,
            answer_text=answer_text,
            usage=llm.last_usage,
        )
        await _emit({"event": "stage", "stage": "generate", "status": "done"})

    # --- Step 6: normalize native citation markup, then strip a trailing source-list block --
    # --- BOTH before verification, not after. ---
    # Verification has to check the text that will actually render. If citations were verified on
    # the raw generated text and the source-list block stripped afterward, a model that put every
    # one of its brackets only inside a trailing "Source URLs:"-style block (and none inline) would
    # pass verification against the raw text, then have that block stripped, and render with zero
    # visible bracket citations despite having "passed". Stripping first closes that gap: the
    # citation check below inspects exactly the string that ends up in the response.
    #
    # Phase 8 round 4: normalization runs FIRST, before the strip above -- gpt-oss frequently cites
    # in its own native "【N†...】" markup instead of this project's "[N]" bracket convention (see
    # app/prompts.py::normalize_native_citation_markup's own docstring), and verify_citations below
    # only ever recognizes "[N]". Order relative to strip_source_list_block does not matter in
    # practice (the two patterns match different, non-overlapping shapes of text), but normalizing
    # first means a native-markup citation inside a would-be-stripped trailing block is rewritten
    # the same way an inline one is, rather than depending on which pass happens to run first.
    answer_text = normalize_native_citation_markup(answer_text)
    answer_text = strip_source_list_block(answer_text)

    # --- Step 7: verify citations. Blocks rendering the generated text on failure. ---
    await _emit({"event": "stage", "stage": "verify", "status": "start"})
    verification = verify_citations(
        answer_text, num_contexts=len(chunks), response_type=candidate_response_type.value
    )
    await _emit({"event": "stage", "stage": "verify", "status": "done", "ok": verification.ok})

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

    # --- Step 8 (Phase 5): freshness. Built from the same retrieved chunks, for ANSWER and
    # --- REFUSAL_ADVICE alike (both are the response types that render generated text and
    # --- citations at all). The notice sentence carries no bracket, so appending it here -- after
    # --- verify_citations already ran in step 7 -- can never change what that check saw.
    #
    # cited_indices is read from `answer_text` as it stands right now: post-strip (step 6) so it
    # matches exactly what verify_citations checked in step 7, and (for an advice response) before
    # or after the DSO redirect makes no difference, since that sentence never carries a bracket.
    # Reused from app/guardrails/citations.py rather than re-implemented here, per that module's
    # own docstring ("the same convention eval/run.py's own parse_cited_indices uses").
    freshness = build_freshness(
        chunks, today=datetime.now(UTC).date(), cited_indices=parse_cited_indices(answer_text)
    )
    notice_text = freshness_notice_text(freshness.notices)
    if notice_text:
        answer_text = f"{answer_text}\n\n{notice_text}"

    trace.get_current_span().set_attribute("response_type", candidate_response_type.value)

    response = AnswerResponse(
        answer=answer_text,
        citations=citations,
        contexts=contexts,
        response_type=candidate_response_type.value,
        refusal_reason=refusal_reason,
        generated_at=datetime.now(UTC),
        freshness=freshness,
    )

    # Phase 8 round 3: cache this fully verified ANSWER/REFUSAL_ADVICE response. Off by default
    # (Settings.SEMANTIC_CACHE_ENABLED=False, cache_version stays None); cache.store() also refuses
    # a non-cacheable response_type itself (defence in depth -- this call site only ever reaches
    # here with ANSWER/REFUSAL_ADVICE anyway, since BLOCKED_UNVERIFIED already returned above).
    if settings.SEMANTIC_CACHE_ENABLED and cache_version is not None:
        await cache.store(pool, query_embedding, response, version=cache_version)

    return response
