"""Pydantic request/response models for the /query endpoint."""

from datetime import datetime

from pydantic import BaseModel, Field

DISCLAIMER = (
    "This is an unofficial tool. It is not legal advice and is not affiliated with USCIS or DHS. "
    "For guidance on your own situation, talk to your DSO or a licensed immigration attorney."
)


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


class AnswerResponse(BaseModel):
    answer: str
    citations: list[Citation]
    contexts: list[RetrievedContext]
    disclaimer: str = DISCLAIMER
    generated_at: datetime
