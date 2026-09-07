"""Cheap-model routing for the Layer 2 advice classifier (Phase 8 round 3): proves that
app/pipeline.py::answer_question hands Layer 2 the routed `classifier_llm` when one is given, and
falls back to the same generator `llm` (byte-identical to before this parameter existed) when it is
not -- app/guardrails/classifier.py::classify_advice itself is completely unchanged; only WHICH LLM
object it receives differs.

Settings.LLM_PROVIDER is deliberately set to "ollama" (not "stub") below: classify_advice's Layer 2
only runs at all when settings.LLM_PROVIDER != "stub" (see that module's own docstring), and this
file needs Layer 2 to actually run so it can prove which LLM object serves it. Every provider
object actually used (embedder, llm, classifier_llm) is still a concrete Python object handed in
directly -- this string is read only by classify_advice's own gate check and telemetry attributes,
never used to construct a provider (that dispatch only happens once, in app/providers/llm.py::
get_llm, which this test never calls).
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings, get_settings
from app.db import make_pool
from app.pipeline import answer_question
from app.providers.embeddings import StubEmbedder
from app.providers.llm import LLM, StubLLM
from app.schemas import ResponseType


class _FixedVerdictLLM(LLM):
    """A fake classifier that always returns the same {"advice": ...} verdict, regardless of the
    question -- so a test can prove definitively which LLM object classify_advice actually called.
    """

    def __init__(self, verdict: bool):
        self._verdict = verdict

    @property
    def model_id(self) -> str:
        return "fixed-verdict-fake"

    async def generate(self, system: str, user: str) -> str:
        del system, user
        return '{"advice": true}' if self._verdict else '{"advice": false}'


@pytest.fixture
def database_url() -> str:
    return os.environ.get("DATABASE_URL", get_settings().DATABASE_URL)


@pytest.fixture
async def pool(database_url):
    p = make_pool(database_url)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def embedder():
    return StubEmbedder(dim=768)


@pytest.fixture
def settings():
    # NO_ANSWER_MAX_DISTANCE=2.0: StubEmbedder's distances carry no semantic meaning (see its own
    # docstring), so the no-answer gate must never fire against them in this test.
    return Settings(LLM_PROVIDER="ollama", EMBED_PROVIDER="stub", NO_ANSWER_MAX_DISTANCE=2.0)


# A question with no Layer-1 advice pattern in it at all (see
# app/guardrails/classifier.py::ADVICE_PATTERNS) -- Layer 1 finds nothing, so Layer 2 is what
# actually decides, which is exactly the code path this file needs to exercise.
_INFORMATIONAL_QUESTION = "How long is the STEM OPT extension?"


async def test_classifier_llm_when_given_is_what_layer_2_actually_calls(pool, embedder, settings):
    """A classifier_llm that always says "advice" must flip the response to REFUSAL_ADVICE, even
    though the main generator (StubLLM) would never produce that JSON shape itself -- proving Layer
    2 was actually served by classifier_llm, not by llm.
    """
    response = await answer_question(
        _INFORMATIONAL_QUESTION,
        pool=pool,
        embedder=embedder,
        llm=StubLLM(),
        settings=settings,
        classifier_llm=_FixedVerdictLLM(verdict=True),
    )

    assert response.response_type == ResponseType.REFUSAL_ADVICE.value


async def test_classifier_llm_omitted_falls_back_to_the_generator_llm_unchanged(
    pool, embedder, settings
):
    """classifier_llm=None (the default, and every call site before this parameter existed) must
    behave EXACTLY as it always has: Layer 2 is handed the main generator `llm` (StubLLM), whose
    reply to the Layer 2 prompt is unparseable JSON, so classify_advice falls back to
    "information" and the response stays a normal ANSWER.
    """
    response = await answer_question(
        _INFORMATIONAL_QUESTION,
        pool=pool,
        embedder=embedder,
        llm=StubLLM(),
        settings=settings,
    )

    assert response.response_type == ResponseType.ANSWER.value


async def test_a_false_classifier_llm_verdict_keeps_the_response_informational(
    pool, embedder, settings
):
    """Symmetric with the "always advice" case above: a routed classifier that always says
    "not advice" must produce a normal ANSWER, confirming the routed model's verdict genuinely
    drives the outcome in both directions, not just one.
    """
    response = await answer_question(
        _INFORMATIONAL_QUESTION,
        pool=pool,
        embedder=embedder,
        llm=StubLLM(),
        settings=settings,
        classifier_llm=_FixedVerdictLLM(verdict=False),
    )

    assert response.response_type == ResponseType.ANSWER.value
