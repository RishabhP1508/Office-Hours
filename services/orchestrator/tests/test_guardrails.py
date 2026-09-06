"""Guardrail pipeline tests: advice refusal, plain factual answers, citation blocking, no-answer,
and clarify -- plus the classifier's rule layer and the citation/source-list helpers in isolation.

DB-backed tests (anything using the `pool` fixture) run against whatever DATABASE_URL points at, the
same convention services/orchestrator/tests/test_hybrid_retrieval.py uses: the CI invariant gate's
ephemeral postgres service, ingested from eval/fixtures/sources (17 chunks); locally, a separate
`officehours_fixtures` database so these tests never touch the live 216-chunk corpus. LLM_PROVIDER
and EMBED_PROVIDER are `stub` throughout except where a test builds its own fake LLM to drive a
specific generated-answer shape (the citation-blocking test) -- neither the real Ollama generator
nor a real judge is ever needed here.

Under the stub embedder, distances have no semantic meaning (it is a content-blind hash -- see
app/providers/embeddings.py::StubEmbedder's own docstring), so `settings` below sets
NO_ANSWER_MAX_DISTANCE=2.0 (the maximum possible cosine distance) for every test except the one
that specifically exercises the no-answer path, which instead sets the threshold low enough
(NO_ANSWER_MAX_DISTANCE=-1.0, below any real distance) to force it deterministically. Calibrating
the real threshold (0.42) against real semantics is a separate, full_corpus-marked test further
down.
"""

import os

import pytest

from app.config import Settings, get_settings
from app.db import hybrid_search, make_pool
from app.guardrails.citations import verify_citations
from app.guardrails.clarifier import CLARIFY_QUESTION, is_too_vague
from app.guardrails.classifier import ADVICE_PATTERNS, classify_advice, rule_based_advice_signal
from app.pipeline import answer_question
from app.prompts import strip_source_list_block
from app.providers.embeddings import OllamaEmbedder, StubEmbedder
from app.providers.llm import LLM, StubLLM
from app.schemas import ResponseType

# --- Fixtures: fixture-corpus DB pool, stub embedder, stub LLM, permissive settings. ---


@pytest.fixture
async def pool():
    settings = get_settings()
    database_url = os.environ.get("DATABASE_URL", settings.DATABASE_URL)
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
def llm():
    return StubLLM()


@pytest.fixture
def settings():
    # NO_ANSWER_MAX_DISTANCE=2.0: the stub embedder's distances carry no semantic meaning (see
    # module docstring), so the real 0.42 threshold would fire on every fixture-corpus query and
    # nothing below would ever reach ANSWER/REFUSAL_ADVICE/BLOCKED_UNVERIFIED at all.
    return Settings(LLM_PROVIDER="stub", EMBED_PROVIDER="stub", NO_ANSWER_MAX_DISTANCE=2.0)


class ExplodingLLM(LLM):
    """A fake LLM that raises if it is ever called, for tests that must prove the generator is
    never reached at all (the no-answer path, and Layer 2 under a stub provider)."""

    async def generate(self, system: str, user: str) -> str:
        raise AssertionError("LLM.generate must not be called")


class FixedAnswerLLM(LLM):
    """A fake LLM that always returns a fixed string, for driving a specific generated-answer
    shape the real (or stub) generator would not reliably produce -- here, a citation index outside
    the retrieved range."""

    def __init__(self, text: str):
        self._text = text

    async def generate(self, system: str, user: str) -> str:
        return self._text


class ExplodingPool:
    """A fake connection pool that raises if any attribute is touched, proving the clarifier path
    never reaches the database at all."""

    def __getattr__(self, name):
        raise AssertionError(f"pool.{name} must not be touched on the CLARIFY path")


class ExplodingEmbedder:
    """A fake embedder that raises if `embed` is ever called, proving the clarifier path never
    reaches the embedding provider at all."""

    async def embed(self, texts):
        raise AssertionError("embedder.embed must not be called on the CLARIFY path")


# --- DoD 1: an advice-seeking query returns REFUSAL_ADVICE with a DSO/attorney redirect. ---


async def test_advice_query_returns_refusal_advice_with_redirect(pool, embedder, llm, settings):
    question = "Should I use OPT now or save it all for after I graduate?"
    response = await answer_question(
        question, pool=pool, embedder=embedder, llm=llm, settings=settings
    )
    assert response.response_type == ResponseType.REFUSAL_ADVICE.value
    assert response.refusal_reason == "query_asks_for_personal_advice"
    lowered = response.answer.lower()
    assert "dso" in lowered or "designated school official" in lowered
    assert "licensed immigration attorney" in lowered


# --- DoD 2: a normal factual query returns ANSWER with a citation among the retrieved contexts. ---


async def test_factual_query_returns_answer_with_citation_in_retrieved_contexts(
    pool, embedder, llm, settings
):
    question = "What is a Form I-515A and when is it issued?"
    response = await answer_question(
        question, pool=pool, embedder=embedder, llm=llm, settings=settings
    )
    assert response.response_type == ResponseType.ANSWER.value
    assert response.refusal_reason is None
    assert response.citations, "expected at least one citation"
    context_ids = {c.chunk_id for c in response.contexts}
    assert any(c.chunk_id in context_ids for c in response.citations)


# --- DoD 3: an answer citing an index outside the retrieved range is BLOCKED, not rendered. ---


async def test_out_of_range_citation_is_blocked_not_rendered(pool, embedder, settings):
    fake_text = "The rule is stated clearly [9], which nobody retrieved for this question."
    fake_llm = FixedAnswerLLM(fake_text)
    question = "What is a Form I-515A and when is it issued?"
    response = await answer_question(
        question, pool=pool, embedder=embedder, llm=fake_llm, settings=settings
    )
    assert response.response_type == ResponseType.BLOCKED_UNVERIFIED.value
    assert response.refusal_reason == "citation_index_out_of_range"
    assert fake_text not in response.answer
    assert "[9]" not in response.answer


# --- DoD 4: a query with no relevant source returns NO_ANSWER; the generator is never called. ---


async def test_no_relevant_source_returns_no_answer_without_calling_the_generator(pool, embedder):
    # NO_ANSWER_MAX_DISTANCE=-1.0: below any real cosine distance (always >= 0), so the no-answer
    # path fires deterministically regardless of what the stub embedder's hash happens to produce --
    # this test is about the mechanism (compare-then-skip-generate), not about semantic calibration
    # (see the full_corpus calibration test further down for that).
    settings = Settings(LLM_PROVIDER="stub", EMBED_PROVIDER="stub", NO_ANSWER_MAX_DISTANCE=-1.0)
    response = await answer_question(
        "What is a Form I-515A and when is it issued?",
        pool=pool,
        embedder=embedder,
        llm=ExplodingLLM(),
        settings=settings,
    )
    assert response.response_type == ResponseType.NO_ANSWER.value
    assert response.refusal_reason == "min_distance_exceeds_threshold"
    assert response.citations == []
    assert response.contexts == []


# --- DoD 5: a too-vague query returns CLARIFY with exactly one question, and never retrieves. ---


async def test_vague_query_returns_clarify_without_retrieving():
    settings = Settings(LLM_PROVIDER="stub", EMBED_PROVIDER="stub")
    response = await answer_question(
        "help",
        pool=ExplodingPool(),
        embedder=ExplodingEmbedder(),
        llm=ExplodingLLM(),
        settings=settings,
    )
    assert response.response_type == ResponseType.CLARIFY.value
    assert response.refusal_reason == "query_too_vague"
    assert response.answer.count("?") == 1
    assert response.citations == []
    assert response.contexts == []


# --- Supplementary: strip_source_list_block ---


def test_strip_source_list_block_removes_trailing_sources_section():
    answer = (
        "The STEM OPT extension is 24 months [1].\n\n"
        "Source URLs:\n"
        "[1] https://www.uscis.gov/some-page"
    )
    stripped = strip_source_list_block(answer)
    assert "http" not in stripped
    assert "Source URLs" not in stripped
    assert "24 months" in stripped


def test_strip_source_list_block_leaves_a_normal_answer_untouched():
    answer = "The STEM OPT extension is 24 months [1]."
    assert strip_source_list_block(answer) == answer


# --- Supplementary: programmatic citation verification (app/guardrails/citations.py) ---


def test_verify_citations_passes_a_well_formed_answer():
    result = verify_citations("It is 24 months [1].", num_contexts=2, response_type="answer")
    assert result.ok is True
    assert result.reason is None


def test_verify_citations_flags_an_out_of_range_index():
    result = verify_citations("It is 24 months [3].", num_contexts=2, response_type="answer")
    assert result.ok is False
    assert result.reason == "citation_index_out_of_range"


def test_verify_citations_flags_an_answer_with_no_citation_at_all():
    result = verify_citations("It is 24 months.", num_contexts=2, response_type="answer")
    assert result.ok is False
    assert result.reason == "answer_missing_citation"


def test_verify_citations_does_not_require_a_citation_on_a_refusal():
    result = verify_citations("Talk to your DSO.", num_contexts=2, response_type="refusal_advice")
    assert result.ok is True


# --- Supplementary: classifier rule layer (Layer 1) ---


def test_rule_layer_flags_a_generic_advice_phrasing():
    assert rule_based_advice_signal("Should I file now or wait until next year?") is True


def test_rule_layer_does_not_flag_a_generic_factual_phrasing():
    assert rule_based_advice_signal("How long is the STEM OPT extension?") is False


async def test_layer2_not_consulted_when_provider_is_stub():
    """The question below deliberately matches none of ADVICE_PATTERNS (Layer 1), so the only way
    this could reach Layer 2 is if the stub-provider gate were missing. Consulting ExplodingLLM at
    all would raise.
    """
    settings = Settings(LLM_PROVIDER="stub")
    result = await classify_advice(
        "What are my odds in the H-1B lottery this year?", llm=ExplodingLLM(), settings=settings
    )
    assert result.decided_by == "model_unavailable"
    assert result.is_advice is False


def test_advice_patterns_never_match_exactly_one_golden_row():
    """Moved from services/orchestrator/tests/test_stub_providers.py -- the advice-vs-information
    decision belongs to app.guardrails.classifier now, not StubLLM (see app/pipeline.py), so this
    guard moved with it. A pattern that fires on exactly one row of eval/golden.jsonl makes that
    one row's classification a tautology (the pattern was, in effect, reading that row's own label
    back), not a measurement -- see the comment above ADVICE_PATTERNS. Two patterns used to do
    exactly this ("my odds" -> only the H-1B-lottery row, "will uscis count" -> only the
    specialty-occupation row) and were removed before this list ever moved here; this guards
    against silently reintroducing one, here or in a future edit.

    Imports eval.run lazily, inside the test body: conftest.py makes the top-level `eval` package
    importable, but importing it only where it is used keeps this module's own import list honest
    about what every other test in this file actually needs.
    """
    from eval.run import load_golden_set

    questions = [row["question"] for row in load_golden_set()]
    for pattern in ADVICE_PATTERNS:
        match_count = sum(1 for q in questions if pattern in q.lower())
        assert match_count != 1, (
            f"pattern {pattern!r} matches exactly one golden row ({match_count} match) -- that "
            "makes the row's classification a tautology, not a measurement; remove or broaden the "
            "pattern"
        )


# --- Supplementary: clarifier (app/guardrails/clarifier.py) ---


@pytest.mark.parametrize("question", ["help", "opt?", "i have a question", "visa"])
def test_clarifier_flags_vague_queries(question):
    assert is_too_vague(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "How long is the STEM OPT extension?",
        "What is the I-983 and who fills it out?",
        "Which form do I file for the OPT work permit?",
    ],
)
def test_clarifier_does_not_flag_specific_queries(question):
    assert is_too_vague(question) is False


def test_clarifier_anchor_rescues_a_short_but_specific_query():
    """Two content words that include a recognized topic anchor ("opt") are specific enough to
    skip straight to retrieval, unlike a bare one-word "opt?"
    (see test_clarifier_flags_vague_queries and app/guardrails/clarifier.py's module docstring for
    why the two are treated differently).
    """
    assert is_too_vague("OPT deadline") is False


def test_clarify_question_contains_exactly_one_question_mark():
    assert CLARIFY_QUESTION.count("?") == 1


# --- full_corpus: calibrates NO_ANSWER_MAX_DISTANCE against the live 216-chunk corpus and the
# --- real nomic-embed-text embeddings. Requires the real corpus (python -m app.ingest,
# --- INGEST_MODE=fetch) and a reachable Ollama -- never runs against the fixture corpus or the
# --- stub embedder, both of which carry no semantic meaning (see module docstring). ---


@pytest.mark.full_corpus
async def test_no_answer_threshold_separates_control_queries_on_the_live_corpus(pool):
    """14 control queries, none drawn from eval/golden.jsonl: 7 in-domain-but-not-golden questions
    about F-1/OPT/STEM OPT/H-1B topics this corpus covers, phrased differently from any golden row,
    and 7 deliberately off-topic questions with nothing to do with immigration. Gates on the
    MINIMUM distance across all retrieved chunks, matching app/pipeline.py -- not the RRF-top-1
    chunk's own distance, which is a noisier, fused-rank quantity (see
    Settings.NO_ANSWER_MAX_DISTANCE's comment in app/config.py).

    The seven in-domain controls measured 0.1563-0.3814 here, comfortably below 0.50.

    The seven off-topic controls split into a tight cluster of six from 0.5251 up (asserted below)
    plus one known, accepted miss: "How much does car insurance cost per month?" measured 0.4370,
    below the threshold. That query is deliberately NOT asserted here -- see
    Settings.NO_ANSWER_MAX_DISTANCE's own comment in app/config.py for why a threshold low enough to
    also catch it would suppress real, answerable golden questions instead, and why prompt rule 3
    ("say plainly when the sources do not cover it") is the accepted mechanism for that one gap
    rather than a lower threshold.

    This is a real, falsifiable check of that calibration: it fails the day the corpus or the
    embedding model changes in a way that invalidates the threshold, which is exactly when someone
    needs to find out.
    """
    settings = get_settings()
    embedder = OllamaEmbedder(base_url=settings.OLLAMA_BASE_URL, model=settings.EMBED_MODEL)

    in_domain_queries = [
        "What happens to my F-1 status during the cap-gap extension period?",
        "How long is my travel signature valid on my I-20?",
        "Can I change my status from F-1 to H-1B while still in the US?",
        "What is H-1B portability and when can I start working for a new employer?",
        "How is the prevailing wage level determined for an H-1B position?",
        "What is the F-1 grace period after finishing OPT?",
        "What happens to my status if my SEVIS record gets terminated?",
    ]
    # The six off-topic controls the threshold is actually asserted against (the cluster from
    # 0.5251 up). "How much does car insurance cost per month?" is deliberately excluded -- see the
    # docstring above -- it is a documented, accepted miss, not an oversight.
    off_topic_queries_above_threshold = [
        "How do I make sourdough bread rise properly?",
        "What is the capital of France?",
        "How much vitamin D should I take daily?",
        "Who won the last World Cup?",
        "How do I fix a leaky faucet?",
        "What programming language is best for making video games?",
    ]

    async def min_distance(question: str) -> float:
        [embedding] = await embedder.embed([question])
        results = await hybrid_search(
            pool, embedding, question, k=settings.RETRIEVAL_TOP_K, rrf_k=60, candidate_pool=20
        )
        assert results, f"expected at least one result for {question!r}"
        return min(r.distance for r in results)

    for question in in_domain_queries:
        distance = await min_distance(question)
        assert distance < settings.NO_ANSWER_MAX_DISTANCE, (
            f"in-domain control {question!r} has min distance {distance}, not below "
            f"NO_ANSWER_MAX_DISTANCE ({settings.NO_ANSWER_MAX_DISTANCE}) -- the threshold no "
            "longer separates in-domain from off-topic on this corpus/embedding model"
        )

    for question in off_topic_queries_above_threshold:
        distance = await min_distance(question)
        assert distance > settings.NO_ANSWER_MAX_DISTANCE, (
            f"off-topic control {question!r} has min distance {distance}, not above "
            f"NO_ANSWER_MAX_DISTANCE ({settings.NO_ANSWER_MAX_DISTANCE}) -- the threshold no "
            "longer separates in-domain from off-topic on this corpus/embedding model"
        )


@pytest.mark.full_corpus
async def test_no_answer_gate_does_not_suppress_a_sparse_form_number_match(pool):
    """Guards the Phase 3 win a semantic-distance-only gate could silently undo (see
    docs/adr/0001-rrf-vs-weighted-blend.md and Settings.NO_ANSWER_MAX_DISTANCE's own comment on the
    I-983 golden row that motivated correcting this threshold from 0.42 to 0.50): a real question
    whose supporting chunk is narrow and rare in the corpus must not get gated to NO_ANSWER just
    because its distance signal is weaker than a common topic's.

    "Form I-515A" is used here, not I-983: it appears in exactly one chunk out of 216 (id 540, "Form
    I-515A", verified with `SELECT id, section_heading FROM documents WHERE content ILIKE
    '%I-515A%'`) and in NO golden question (`grep -oE "I-[0-9]+[A-Z]?" eval/golden.jsonl` finds only
    I-20, I-765, and I-983), so this is a behavioral guard, not a test fitted to the eval.
    """
    settings = get_settings()
    embedder = OllamaEmbedder(base_url=settings.OLLAMA_BASE_URL, model=settings.EMBED_MODEL)
    question = "What is a Form I-515A and when is it issued?"

    [embedding] = await embedder.embed([question])
    results = await hybrid_search(
        pool, embedding, question, k=settings.RETRIEVAL_TOP_K, rrf_k=60, candidate_pool=20
    )
    assert results, f"expected at least one result for {question!r}"
    assert any(r.section_heading == "Form I-515A" for r in results), (
        "expected the sparse 'Form I-515A' chunk to be retrieved at all for this question -- got "
        f"{[(r.id, r.section_heading) for r in results]}"
    )

    distance = min(r.distance for r in results)
    assert distance <= settings.NO_ANSWER_MAX_DISTANCE, (
        f"min distance {distance} for a real, narrowly-supported question exceeds "
        f"NO_ANSWER_MAX_DISTANCE ({settings.NO_ANSWER_MAX_DISTANCE}) -- a semantic-distance gate "
        "is wrongly suppressing a real answer that rests on a sparse, rare chunk"
    )
