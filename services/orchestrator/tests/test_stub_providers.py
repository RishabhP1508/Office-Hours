"""Unit tests for the deterministic stub providers used by the CI invariant gate.

No live stack, no network, no database: these exercise app.providers.llm.StubLLM and
app.providers.embeddings.StubEmbedder directly, the same objects EVAL_MODE=ci wires up through
LLM_PROVIDER=stub / EMBED_PROVIDER=stub (see get_llm / get_embedder).
"""

import math

import pytest

from app.config import Settings
from app.prompts import SYSTEM_PROMPT, build_user_prompt
from app.providers.embeddings import StubEmbedder, get_embedder
from app.providers.llm import _STUB_ADVICE_PATTERNS, StubLLM, get_llm


def _user_prompt(question: str, num_chunks: int) -> str:
    chunks = [
        {"content": f"chunk body {i}", "citation_url": f"https://example.gov/page-{i}"}
        for i in range(1, num_chunks + 1)
    ]
    return build_user_prompt(question, chunks)


@pytest.mark.asyncio
async def test_stub_llm_is_deterministic_across_calls():
    llm = StubLLM()
    user = _user_prompt("How long is the STEM OPT extension?", num_chunks=3)
    first = await llm.generate(SYSTEM_PROMPT, user)
    second = await llm.generate(SYSTEM_PROMPT, user)
    assert first == second


@pytest.mark.asyncio
async def test_stub_llm_cites_only_indices_present_in_the_prompt():
    llm = StubLLM()
    user = _user_prompt("How long is the STEM OPT extension?", num_chunks=1)
    answer = await llm.generate(SYSTEM_PROMPT, user)
    assert "[1]" in answer
    assert "[2]" not in answer  # only one context was offered; must never cite beyond it


@pytest.mark.asyncio
async def test_stub_llm_cites_two_indices_when_two_contexts_are_present():
    llm = StubLLM()
    user = _user_prompt("How long is the STEM OPT extension?", num_chunks=5)
    answer = await llm.generate(SYSTEM_PROMPT, user)
    assert "[1, 2]" in answer


@pytest.mark.asyncio
async def test_stub_llm_never_cites_when_no_context_was_retrieved():
    llm = StubLLM()
    user = _user_prompt("How long is the STEM OPT extension?", num_chunks=0)
    answer = await llm.generate(SYSTEM_PROMPT, user)
    assert "[1]" not in answer
    assert "[2]" not in answer


@pytest.mark.asyncio
async def test_stub_llm_refuses_advice_seeking_questions():
    llm = StubLLM()
    user = _user_prompt("Should I use OPT now or save it for after I graduate?", num_chunks=3)
    answer = await llm.generate(SYSTEM_PROMPT, user)
    assert "designated school official" in answer.lower()
    assert "licensed immigration attorney" in answer.lower()


@pytest.mark.asyncio
async def test_stub_llm_does_not_refuse_factual_questions():
    llm = StubLLM()
    user = _user_prompt("How long is the STEM OPT extension?", num_chunks=3)
    answer = await llm.generate(SYSTEM_PROMPT, user)
    assert "designated school official" not in answer.lower()


@pytest.mark.asyncio
async def test_stub_llm_refusal_decision_ignores_everything_but_the_question_text():
    """The stub must never see the golden set's is_advice label -- generate()'s signature only
    ever takes (system, user) strings, so the only way it could behave differently for two calls
    with the same question is if it looked outside those two strings. Passing a different
    `system` prompt for the same `user` prompt must not change the outcome.
    """
    llm = StubLLM()
    user = _user_prompt("Should I use OPT now or save it for after I graduate?", num_chunks=2)
    first = await llm.generate(SYSTEM_PROMPT, user)
    second = await llm.generate("a completely different system prompt", user)
    assert first == second


def test_get_llm_dispatches_stub_provider():
    settings = Settings(LLM_PROVIDER="stub")
    assert isinstance(get_llm(settings), StubLLM)


@pytest.mark.asyncio
async def test_stub_embedder_is_deterministic_across_calls():
    embedder = StubEmbedder(dim=16)
    [first] = await embedder.embed(["How long is the STEM OPT extension?"])
    [second] = await embedder.embed(["How long is the STEM OPT extension?"])
    assert first == second


@pytest.mark.asyncio
async def test_stub_embedder_produces_the_configured_dimension():
    embedder = StubEmbedder(dim=768)
    [vector] = await embedder.embed(["some text"])
    assert len(vector) == 768


@pytest.mark.asyncio
async def test_stub_embedder_differs_for_different_text():
    embedder = StubEmbedder(dim=32)
    [a] = await embedder.embed(["How long is the STEM OPT extension?"])
    [b] = await embedder.embed(["What is the I-983?"])
    assert a != b


@pytest.mark.asyncio
async def test_stub_embedder_returns_a_unit_vector():
    embedder = StubEmbedder(dim=32)
    [vector] = await embedder.embed(["some text"])
    norm = math.sqrt(sum(v * v for v in vector))
    assert norm == pytest.approx(1.0, abs=1e-9)


@pytest.mark.asyncio
async def test_stub_embedder_handles_empty_input_list():
    embedder = StubEmbedder(dim=32)
    assert await embedder.embed([]) == []


def test_get_embedder_dispatches_stub_provider():
    settings = Settings(EMBED_PROVIDER="stub", EMBED_DIM=16)
    embedder = get_embedder(settings)
    assert isinstance(embedder, StubEmbedder)
    assert embedder._dim == 16


def test_stub_advice_patterns_never_match_exactly_one_golden_row():
    """A pattern that fires on exactly one row of eval/golden.jsonl makes that one row's refusal
    classification a tautology (the pattern was, in effect, reading that row's own label back),
    not a measurement -- see the comment above app/providers/llm.py::_STUB_ADVICE_PATTERNS. Two
    patterns used to do exactly this ("my odds" -> only row 5, "will uscis count" -> only row 8)
    and were removed; this guards against silently reintroducing one, here or in a future edit.

    Imports eval.run lazily, inside the test body: conftest.py makes the top-level `eval` package
    importable, but importing it only where it is used keeps this module's own import list honest
    about what every other test in this file actually needs.
    """
    from eval.run import load_golden_set

    questions = [row["question"] for row in load_golden_set()]
    for pattern in _STUB_ADVICE_PATTERNS:
        match_count = sum(1 for q in questions if pattern in q.lower())
        assert match_count != 1, (
            f"pattern {pattern!r} matches exactly one golden row ({match_count} match) -- that "
            "makes the row's refusal classification a tautology, not a measurement; remove or "
            "broaden the pattern"
        )
