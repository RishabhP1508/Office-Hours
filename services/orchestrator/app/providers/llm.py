"""LLM provider interface.

Three implementations behind one interface: local Ollama for dev, a hosted API for production,
and a deterministic stub for the CI invariant gate (see eval/run.py's EVAL_MODE=ci / --ci and
docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md). The stub never touches the network and
never varies between runs, which is what lets CI score citation mapping, refusal bookkeeping, and
error handling without a GPU, a hosted API key, or a real corpus.
"""

import re
from abc import ABC, abstractmethod

import httpx

from app.config import Settings


class LLM(ABC):
    """Generates text from a system prompt and a user prompt."""

    @abstractmethod
    async def generate(self, system: str, user: str) -> str:
        raise NotImplementedError


class OllamaLLM(LLM):
    def __init__(
        self, base_url: str, model: str, timeout_seconds: float, keep_alive: str, think: bool
    ):
        self._base_url = base_url.rstrip("/")
        self._model = model
        # Ollama can take well over a minute to answer -- an advice-seeking question in
        # particular can make the local model deliberate at length before responding -- so the
        # read timeout comes from Settings.LLM_TIMEOUT_SECONDS rather than httpx's 5s default.
        # Connect timeout stays short: a dead/unreachable host should fail fast, only the
        # response itself is allowed to take a long time.
        self._timeout = httpx.Timeout(timeout_seconds, connect=10.0)
        self._keep_alive = keep_alive
        self._think = think

    async def generate(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    # Sent on every request rather than relying on the host's own OLLAMA_KEEP_ALIVE
                    # env var, so a fresh clone behaves the same way without extra host setup (see
                    # Settings.OLLAMA_KEEP_ALIVE).
                    "keep_alive": self._keep_alive,
                    # Off by default (Settings.OLLAMA_THINK): the generator is a reasoning model
                    # that will otherwise spend its whole output budget on a "thinking" block and
                    # get cut off before writing any content, returning HTTP 200 with empty
                    # content. A grounded answer built from retrieved context does not need
                    # chain-of-thought.
                    "think": self._think,
                },
            )
            if response.status_code != 200:
                # Not response.raise_for_status(): that raises httpx.HTTPStatusError whose
                # default message omits the response body, which is exactly where Ollama puts
                # the real cause (context overflow, OOM, a model unload mid-request). Reading the
                # body here and putting it in the message means it survives however this
                # exception gets logged or converted upstream (see app/main.py's /query handler).
                raise RuntimeError(
                    f"Ollama /api/chat returned HTTP {response.status_code} for model "
                    f"{self._model!r}: {response.text}"
                )
            data = response.json()
        message = data.get("message", {})
        content = message.get("content")
        # An empty or whitespace-only string is just as unusable as a missing field: it renders as
        # a blank answer that every downstream consumer (the API response, the eval harness, the
        # judge) would otherwise happily accept as a real one. done_reason and eval_count are
        # surfaced explicitly because they are the evidence that distinguishes the likely causes --
        # a token/context-length cutoff (done_reason="length") from a model unload or other abrupt
        # stop -- without having to go dig the full payload out of a log line.
        if content is None or not content.strip():
            done_reason = data.get("done_reason")
            eval_count = data.get("eval_count")
            raise RuntimeError(
                f"Ollama /api/chat returned empty content for model {self._model!r} "
                f"(done_reason={done_reason!r}, eval_count={eval_count!r}). Full response: {data}"
            )
        return content


_STUB_CONTEXT_RE = re.compile(r"^\[(\d+)\] Source:", re.MULTILINE)
_STUB_QUESTION_RE = re.compile(r"Question:\s*(.*)", re.DOTALL)

# Deterministic, question-text-only heuristic for whether StubLLM refuses instead of answering.
# This is NOT the Phase 4 guardrail classifier (app/guardrails/classifier.py) and it is never
# handed eval/golden.jsonl's is_advice label -- StubLLM.generate only ever sees the system/user
# prompt strings, the same inputs a real provider gets. Keeping the stub blind to the label is
# what keeps the CI refusal metrics an honest check of the plumbing (retrieval -> prompt ->
# generation -> citation/refusal bookkeeping) rather than a self-fulfilling check against a label
# handed to the thing being measured.
#
# Being blind to the label is not enough on its own, though: a pattern that happens to match
# exactly one row in the 21-row golden set makes that one row's classification a tautology, not a
# measurement, whatever the pattern's intent was. Four patterns that were here before turned out to
# do exactly that against the current golden set -- "my odds" and "will uscis count" each matched
# only the one row they were clearly worded after ("What are my odds in the H-1B lottery this
# year?", "Will USCIS count it as a specialty occupation?"), and "should my" and "what should i do"
# each matched only one row too, coincidentally. All four are gone; every pattern remaining below
# was checked against eval/golden.jsonl and matches either zero or several rows, never exactly one
# (see services/orchestrator/tests/test_stub_providers.py::
# test_stub_advice_patterns_never_match_exactly_one_golden_row, which re-checks this on every run so
# a future edit can't quietly reintroduce a single-row match). This is still only a rough,
# deliberately generic heuristic ("should I", "which is better") -- see eval/run.py's
# CI_STUB_DERIVED_REPORTED_METRICS for why the CI metrics it feeds are reported, not gated.
_STUB_ADVICE_PATTERNS = (
    "should i",
    "should we",
    "which is better",
    "which one should",
    "which visa is best",
    "which status is best",
    "is it better to",
    "will i be approved",
    "my chances",
    "what would you do",
    "can you recommend",
    "would you recommend",
)

# Deliberately contains phrasing eval/run.py's CI_REFUSAL_PATTERNS (see that module) is built to
# recognize, so a CI run that reaches this path has something real to detect -- see that module's
# own comment for what the detector approximates and why it exists.
_STUB_REFUSAL_TEXT = (
    "Whether to do this, which option is better, or whether something will be approved is a "
    "judgment call this tool will not make. Talk to your designated school official (DSO) or a "
    "licensed immigration attorney about your specific situation -- they can weigh the facts that "
    "apply to you. [StubLLM deterministic refusal, generated for the CI invariant gate -- see "
    "app/providers/llm.py.]"
)


def _stub_looks_like_advice(question: str) -> bool:
    lowered = question.lower()
    return any(pattern in lowered for pattern in _STUB_ADVICE_PATTERNS)


def _stub_answer_text(num_contexts: int) -> str:
    prefix = (
        "Stub answer for the CI invariant gate: generated deterministically by StubLLM (see "
        "app/providers/llm.py), not a real generator, and carries no information about the real "
        "answer -- it exists only to exercise retrieval, citation, and refusal plumbing."
    )
    if num_contexts <= 0:
        return (
            f"{prefix} No context was retrieved for this question, so the sources do not cover it."
        )
    cited = [1] if num_contexts >= 1 else []
    if num_contexts >= 2:
        cited.append(2)
    bracket = "[" + ", ".join(str(i) for i in cited) + "]"
    return f"{prefix} The retrieved context addresses this question {bracket}."


class StubLLM(LLM):
    """Deterministic, network-free stand-in for a real generator, selected by LLM_PROVIDER=stub.

    Same input always gives the same output: no randomness, no clock, no network call. Behavior
    depends only on the `user` prompt handed to `generate` (built by
    app/prompts.py::build_user_prompt), never on `system`, and never on anything outside the two
    prompt strings -- in particular, never on eval/golden.jsonl's is_advice label (see
    _STUB_ADVICE_PATTERNS above for why that matters).

    Refuses (see _STUB_REFUSAL_TEXT) when the question text matches _STUB_ADVICE_PATTERNS,
    exercising the refusal path the way a real advice-seeking question would. Otherwise, cites a
    fixed subset of the retrieved context indices ([1], and [2] when a second context is present)
    in the `format_context` bracket numbering, so the citation-hallucination and
    unreferenced-citation checks have something real to parse. Citing only indices that are
    actually present in the prompt is what keeps citation_hallucination_rate at zero by
    construction; it is never coerced to zero after the fact.
    """

    async def generate(self, system: str, user: str) -> str:
        del system  # unused: the stub's behavior depends only on the user prompt (see docstring)
        question_match = _STUB_QUESTION_RE.search(user)
        question = question_match.group(1).strip() if question_match else ""
        num_contexts = len(_STUB_CONTEXT_RE.findall(user))
        if _stub_looks_like_advice(question):
            return _STUB_REFUSAL_TEXT
        return _stub_answer_text(num_contexts)


class HostedLLM(LLM):
    """Seam for a hosted LLM API in production. Not wired up in Phase 0."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key
        self._model = model

    async def generate(self, system: str, user: str) -> str:
        raise NotImplementedError(
            "HostedLLM is a seam for a production LLM API and is not implemented yet. "
            "Set LLM_PROVIDER=ollama for local development."
        )


def get_llm(settings: Settings) -> LLM:
    if settings.LLM_PROVIDER == "ollama":
        return OllamaLLM(
            base_url=settings.OLLAMA_BASE_URL,
            model=settings.LLM_MODEL,
            timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
            keep_alive=settings.OLLAMA_KEEP_ALIVE,
            think=settings.OLLAMA_THINK,
        )
    if settings.LLM_PROVIDER == "hosted":
        return HostedLLM(model=settings.LLM_MODEL)
    if settings.LLM_PROVIDER == "stub":
        return StubLLM()
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.LLM_PROVIDER!r}")
