"""Pydantic request/response models for the /query endpoint."""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

DISCLAIMER = (
    "This is an unofficial tool. It is not legal advice and is not affiliated with USCIS or DHS. "
    "For guidance on your own situation, talk to your DSO or a licensed immigration attorney."
)


class ResponseType(StrEnum):
    """The five shapes a /query response can take, set by the pipeline itself
    (app/pipeline.py), not inferred after the fact by pattern-matching the answer's prose.

    This exists so eval/run.py (and anything else reading a response) can read the pipeline's own
    decision directly -- exactly the "prefer a programmatic check over a model's/regex's opinion"
    principle ARCHITECTURE.md applies everywhere else, applied here to "what kind of response is
    this" instead of just "is this citation valid".

    REFUSAL_ADVICE and NO_ANSWER are deliberately two separate members, never collapsed into one
    generic "refused" value: ARCHITECTURE.md's "The system says when it does not know" section is
    explicit that refusing an advice-seeking question and saying the sources don't cover a topic are
    two different failure modes, each with its own test, and this field is where that distinction is
    recorded end to end.

        ANSWER              a cited factual answer
        REFUSAL_ADVICE      the query asked the system to resolve a personal decision
        CLARIFY             the query was too vague to retrieve against; one question returned
        NO_ANSWER           retrieval found nothing relevant enough; sources do not cover it
        BLOCKED_UNVERIFIED  an answer was generated but failed citation verification
    """

    ANSWER = "answer"
    REFUSAL_ADVICE = "refusal_advice"
    CLARIFY = "clarify"
    NO_ANSWER = "no_answer"
    BLOCKED_UNVERIFIED = "blocked_unverified"


class QueryRequest(BaseModel):
    question: str = Field(min_length=1)


class Citation(BaseModel):
    source_url: str
    chunk_id: int
    snippet: str


class RetrievedContext(BaseModel):
    """The full text of one retrieved chunk, in retrieval order.

    Position in this list (1-based) is exactly the bracket number ([1], [2], ...) the model was
    given in the prompt (see prompts.py::format_context) and may reference in the answer, and it
    lines up one-to-one with `citations` below since both come from the same retrieval call. This
    is what lets eval/run.py check that every bracketed reference in an answer maps to something
    actually retrieved, and it is also what RAGAS's faithfulness and context_precision metrics score
    against, instead of the 240-character citation snippet.
    """

    chunk_id: int
    source_url: str
    section_heading: str
    content: str


class FreshnessSource(BaseModel):
    """One distinct retrieved source's own freshness bookkeeping (Phase 5). `source_url` here is
    the citation URL (resolved_url when present, source_url otherwise -- see
    app/pipeline.py::_citation_url), so it lines up with `Citation.source_url` above.
    """

    source_url: str
    page_last_updated: date | None
    last_verified_at: datetime
    fetched_at: datetime
    rule_effective_date: date | None


class FreshnessNotice(BaseModel):
    """A distinct retrieved source states a dated rule -- for example the DHS fixed-period-of-
    admission final rule, effective 2026-09-15 -- AND qualifies to be surfaced in the rendered
    answer (see app/guardrails/freshness.py::build_freshness for the two qualifying conditions).
    `in_effect` is `rule_effective_date <= as_of`, computed once in build_freshness so every
    consumer (the API response, the appended answer text) agrees on the same verdict. `reason`
    records which condition qualified this notice: `"top_ranked"` (this source's chunk was the
    single highest-ranked retrieved chunk) or `"cited"` (the generated answer's bracket citations
    referenced a chunk from this source). A source can satisfy both; build_freshness reports
    `"top_ranked"` in that case (see its docstring).
    """

    source_url: str
    rule_effective_date: date
    in_effect: bool
    reason: Literal["top_ranked", "cited"]


class Freshness(BaseModel):
    """Built by app/guardrails/freshness.py::build_freshness from the chunks a query actually
    retrieved; attached to ANSWER and REFUSAL_ADVICE responses only (see app/pipeline.py). `as_of`
    is the date the answer was generated, so a stored or cached response still states which date
    its freshness claims were true as of.
    """

    as_of: date
    sources: list[FreshnessSource]
    notices: list[FreshnessNotice]


class AnswerResponse(BaseModel):
    """`response_type` and `refusal_reason` are Phase 4 additions, added without removing or
    renaming any existing field: eval/run.py reads `answer`, `citations`, and `contexts` directly,
    and a shape change to those would corrupt the Phase-to-phase comparison it does against past
    runs. `freshness` is a Phase 5 addition, added the same way: ADD ONLY, never rename/remove/
    reorder an existing field.

    `response_type` is exactly one of the `ResponseType` values on every response (see that enum's
    docstring for what each value means and why REFUSAL_ADVICE and NO_ANSWER are kept separate).
    `refusal_reason` is a short, machine-readable string naming why the response is not a plain
    ANSWER (for example "query_too_vague", "answer_missing_citation"); it is always `None` for
    ANSWER. `freshness` is `None` for CLARIFY, NO_ANSWER, and BLOCKED_UNVERIFIED -- those responses
    carry no retrieved chunks worth reporting freshness for (see app/pipeline.py).
    """

    answer: str
    citations: list[Citation]
    contexts: list[RetrievedContext]
    disclaimer: str = DISCLAIMER
    generated_at: datetime
    response_type: str
    refusal_reason: str | None = None
    freshness: Freshness | None = None
