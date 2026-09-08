"""Red-team fix (2026-09-07): QueryRequest.question has no maximum length, so a 60,000-character
question reached the embedder and failed with a 502 that leaked the embedding provider's own
internal config variable name. This file tests both halves of the fix:

  1. app/schemas.py::QueryRequest.question now carries max_length=MAX_QUESTION_LENGTH (4000, see
     that constant's own doc comment for the full derivation from EMBED_GGUF_N_CTX=2048) -- a
     Pydantic-level guarantee, exercised directly against the model.
  2. app/main.py's custom RequestValidationError handler replaces FastAPI's DEFAULT handler (which
     echoes the raw, oversized submitted value straight back in the response body) with one plain,
     actionable sentence that repeats neither the submitted value nor any internal exception
     detail -- exercised end to end through TestClient(app), the same convention
     tests/test_query.py's own `stub_client` fixture uses.

Every "no leaked detail" assertion here checks a PROPERTY (does the body contain any of a list of
internal-detail markers), never a single matched string, per CLAUDE.md's own instruction on this
point.
"""

import pydantic
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import MAX_QUESTION_LENGTH, QueryRequest

# --- Pydantic-level: QueryRequest.question's own max_length. ---


def test_query_request_accepts_a_question_at_exactly_the_max_length():
    request = QueryRequest(question="a" * MAX_QUESTION_LENGTH)
    assert len(request.question) == MAX_QUESTION_LENGTH


def test_query_request_rejects_a_question_one_character_over_the_max_length():
    with pytest.raises(pydantic.ValidationError):
        QueryRequest(question="a" * (MAX_QUESTION_LENGTH + 1))


def test_query_request_rejects_the_60000_character_reproduction_from_the_incident_report():
    """The exact input size that produced the leaking 502 in production."""
    with pytest.raises(pydantic.ValidationError):
        QueryRequest(question="a" * 60_000)


# --- HTTP-level: the custom RequestValidationError handler (app/main.py). ---

# Real Go/Python module names, internal exception types, and config variable names that must never
# appear in a response body a caller sees -- checked as a set of properties, not by matching the one
# example string from the incident report ("Failed to answer question (ValueError): Input is 30007
# tokens, over the 2048-token GGUF context budget (EMBED_GGUF_N_CTX)...").
_FORBIDDEN_DETAIL_MARKERS = (
    "ValueError",
    "Traceback",
    "EMBED_GGUF_N_CTX",
    "GGUFEmbedder",
    "app.pipeline",
    "app.main",
    "app.schemas",
    "pydantic",
    "string_too_long",
    ".py",
    "site-packages",
)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_post_query_with_an_oversized_question_returns_422_with_no_internal_detail(client):
    oversized = "a" * (MAX_QUESTION_LENGTH + 1)
    response = client.post("/query", json={"question": oversized})

    assert response.status_code == 422
    body = response.json()
    rendered = str(body)

    assert oversized not in rendered, "the oversized question must never be echoed back verbatim"
    for marker in _FORBIDDEN_DETAIL_MARKERS:
        assert marker not in rendered, f"response body leaked internal detail ({marker!r}): {body}"
    assert "too long" in body["detail"].lower()
    assert str(MAX_QUESTION_LENGTH) in body["detail"]


def test_post_query_stream_with_an_oversized_question_returns_422_with_no_internal_detail(client):
    """POST /query/stream shares QueryRequest as its own request body -- FastAPI's dependency
    validation runs (and can fail) identically before either route's own handler body ever
    executes, so the same clean 422 applies here too, not a stream carrying a leaking error event.
    """
    oversized = "a" * (MAX_QUESTION_LENGTH + 1)
    response = client.post("/query/stream", json={"question": oversized})

    assert response.status_code == 422
    body = response.json()
    rendered = str(body)

    assert oversized not in rendered
    for marker in _FORBIDDEN_DETAIL_MARKERS:
        assert marker not in rendered, f"response body leaked internal detail ({marker!r}): {body}"
    assert "too long" in body["detail"].lower()


def test_post_query_with_the_60000_character_reproduction_returns_422_not_a_502(client):
    """The exact incident: 60,000 characters used to reach the embedder and come back as a 502
    naming the embedding provider's own internal config variable. It must now never reach the
    embedder at all -- rejected at the Pydantic layer as a clean 422 instead.
    """
    response = client.post("/query", json={"question": "a" * 60_000})

    assert response.status_code == 422
    rendered = str(response.json())
    assert "60000" not in rendered  # the raw character/token count is never echoed either
    for marker in _FORBIDDEN_DETAIL_MARKERS:
        assert marker not in rendered, f"response body leaked internal detail ({marker!r})"


def test_post_query_with_an_empty_question_still_returns_a_clean_422():
    """A DIFFERENT validation failure (min_length=1, not max_length) must still get the generic
    clean message, not the length-specific one, and must still leak no internal detail -- proving
    the handler's specialization is additive, not a regression for the pre-existing empty-question
    case.
    """
    with TestClient(app) as client:
        response = client.post("/query", json={"question": ""})

    assert response.status_code == 422
    body = response.json()
    rendered = str(body)
    for marker in _FORBIDDEN_DETAIL_MARKERS:
        assert marker not in rendered, f"response body leaked internal detail ({marker!r}): {body}"
    assert "detail" in body
    assert isinstance(body["detail"], str)
