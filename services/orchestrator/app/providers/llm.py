"""LLM provider interface.

Implementations behind one interface: local Ollama for dev, Ollama Cloud and an OpenAI-compatible
hosted endpoint (NVIDIA NIM) for production, a FallbackLLM that chains them, and a deterministic
stub for the CI invariant gate (see eval/run.py's EVAL_MODE=ci / --ci and
docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md). The stub never touches the network and
never varies between runs, which is what lets CI score citation mapping, refusal bookkeeping, and
error handling without a GPU, a hosted API key, or a real corpus.

Production generator resolution (Phase 8): PRIMARY is Ollama Cloud (gpt-oss:120b at
https://ollama.com, `Authorization: Bearer $OLLAMA_API_KEY`) -- still an OllamaLLM, since Ollama
Cloud speaks the same /api/chat shape as local Ollama; only the base URL, the bearer key, and
whether keep_alive/think are sent differ (see OllamaLLM's `is_cloud` flag below). FALLBACK is
NVIDIA, OpenAI-compatible at https://integrate.api.nvidia.com/v1/chat/completions
(`Authorization: Bearer $NVIDIA_API_KEY`-shaped, wired up as Settings.LLM_FALLBACK_API_KEY),
model `openai/gpt-oss-20b`, served by OpenAICompatLLM. get_llm() wires both up behind one
FallbackLLM when Settings.LLM_FALLBACK_PROVIDER is set; local dev (LLM_FALLBACK_PROVIDER="", the
default) is untouched and get_llm() returns the bare OllamaLLM exactly as it always has.
"""

import logging
import re
from abc import ABC, abstractmethod

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class LLM(ABC):
    """Generates text from a system prompt and a user prompt."""

    @property
    def model_id(self) -> str:
        """Human-readable model identifier for logging (FallbackLLM's `llm.served_by` line and the
        generator/judge family guard, see resolve_model_family below). The ABC default is a
        placeholder; every concrete subclass below overrides it with its real, configured model.
        """
        return "unknown"

    @property
    def last_usage(self) -> dict | None:
        """REAL token counts (`{"prompt_tokens": int | None, "completion_tokens": int | None}`)
        from the most recently completed `generate()` call, or `None` when the provider reports no
        usage at all (StubLLM, and any provider before its first successful call). Phase 8 round 4:
        backs the Langfuse token-usage panel (app/langfuse_telemetry.py) -- NEVER an estimate, only
        what the provider's own response actually reported. The ABC default is `None`; a concrete
        provider that DOES report usage overrides this with a real, per-instance value updated at
        the end of its own `generate()`.
        """
        return None

    @abstractmethod
    async def generate(self, system: str, user: str) -> str:
        raise NotImplementedError


class OllamaLLM(LLM):
    """Speaks Ollama's /api/chat shape. Used for BOTH local Ollama (dev) and Ollama Cloud
    (production primary): the wire shape is identical, only the base URL, an optional bearer key,
    and whether keep_alive/think are sent differ -- see `api_key` and `is_cloud` below. This is
    deliberately one class rather than a near-identical second one for the cloud case.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float,
        keep_alive: str,
        think: bool,
        api_key: str = "",
        is_cloud: bool = False,
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
        # Empty by default (Settings.OLLAMA_API_KEY): local Ollama needs no auth, so no
        # Authorization header is sent at all unless a key is actually configured. Ollama Cloud
        # requires `Authorization: Bearer <key>`.
        self._api_key = api_key
        # False by default (Settings.OLLAMA_CLOUD), which is what keeps a fresh local clone's
        # behavior byte-for-byte identical to before this flag existed: keep_alive and think are
        # local-Ollama-only concerns (see the two fields' own comments in app/config.py). This
        # project HAS probed Ollama Cloud's real /api/chat behavior for both fields (see
        # docs/reports/phase-8.md): sending `{"model": "gpt-oss:120b-cloud", "think": false,
        # "keep_alive": "60m", "stream": false}` returned HTTP 200 with a non-empty `content` (190
        # characters) AND a populated `thinking` field (243 characters) -- both fields are accepted
        # without error, but `"think": false` does NOT suppress gpt-oss's reasoning block the way it
        # does locally for qwen3.5, and content came back non-empty regardless. So when
        # is_cloud=True, both fields are still omitted entirely -- not because either one is
        # rejected (neither is), but because keep_alive is meaningless against a hosted endpoint
        # (Ollama Cloud manages its own model lifecycle) and think has nothing to suppress and
        # cannot cause the empty-answer failure mode either way, so there is nothing to gain by
        # sending it.
        self._is_cloud = is_cloud
        self._last_usage: dict | None = None

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def last_usage(self) -> dict | None:
        return self._last_usage

    async def generate(self, system: str, user: str) -> str:
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        if not self._is_cloud:
            # Sent on every local request rather than relying on the host's own
            # OLLAMA_KEEP_ALIVE env var, so a fresh clone behaves the same way without extra host
            # setup (see Settings.OLLAMA_KEEP_ALIVE), and to disable the reasoning model's
            # "thinking" block, which otherwise consumes the whole output budget and returns HTTP
            # 200 with empty content (see Settings.OLLAMA_THINK). Neither field is sent to Ollama
            # Cloud -- see this class's docstring and the is_cloud comment in __init__.
            payload["keep_alive"] = self._keep_alive
            payload["think"] = self._think
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/api/chat",
                json=payload,
                headers=headers,
            )
            if response.status_code != 200:
                # Not response.raise_for_status(): that raises httpx.HTTPStatusError whose
                # default message omits the response body, which is exactly where Ollama puts
                # the real cause (context overflow, OOM, a model unload mid-request). Reading the
                # body here and putting it in the message means it survives however this
                # exception gets logged or converted upstream (see app/main.py's /query handler),
                # and this RuntimeError is also what tells FallbackLLM below to move on to the
                # next provider on a 5xx/429 response.
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
        # Phase 8 round 4: real token counts, straight from Ollama's own response -- never
        # estimated. prompt_eval_count is the prompt/context tokens, eval_count is the completion
        # tokens Ollama actually generated; either can be absent (an older Ollama build, or a
        # response shape this project has not seen), in which case `last_usage` reports None for
        # that field rather than a fabricated number.
        self._last_usage = {
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
        }
        return content


class OpenAICompatLLM(LLM):
    """Speaks the OpenAI-compatible `/v1/chat/completions` shape (NVIDIA NIM and similar hosted
    endpoints). `url` is the FULL completions endpoint, not a base URL this class appends a path
    onto -- NVIDIA's own docs hand callers the complete URL
    (https://integrate.api.nvidia.com/v1/chat/completions), and Settings.LLM_FALLBACK_BASE_URL is
    documented in .env.example to hold exactly that.
    """

    def __init__(self, url: str, model: str, api_key: str, timeout_seconds: float):
        self._url = url
        self._model = model
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_seconds, connect=10.0)
        self._last_usage: dict | None = None

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def last_usage(self) -> dict | None:
        return self._last_usage

    async def generate(self, system: str, user: str) -> str:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._url,
                headers=headers,
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                },
            )
            if response.status_code != 200:
                # See OllamaLLM's identical note: the response body carries the real cause, and
                # this RuntimeError is what tells FallbackLLM to move on (5xx, 429, or any other
                # non-200).
                raise RuntimeError(
                    f"{self._url} returned HTTP {response.status_code} for model "
                    f"{self._model!r}: {response.text}"
                )
            data = response.json()
        choices = data.get("choices") or []
        content = choices[0].get("message", {}).get("content") if choices else None
        if content is None or not content.strip():
            raise RuntimeError(
                f"{self._url} returned empty content for model {self._model!r}. "
                f"Full response: {data}"
            )
        # Phase 8 round 4: real token counts from the OpenAI-compatible `usage` block (NVIDIA's
        # endpoint, and any other OpenAI-compatible provider wired up here, both return one) --
        # never estimated. `usage` can be absent on a provider that does not report it, in which
        # case `last_usage` reports None for the fields it did not provide rather than a
        # fabricated number.
        usage = data.get("usage") or {}
        self._last_usage = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }
        return content


class FallbackLLM(LLM):
    """Tries each `(name, LLM)` pair in `providers`, in order, and returns the first one that
    succeeds. Exactly one attempt per provider -- this is failover, not retry-with-backoff, so a
    chain of N providers makes at most N calls total for one `generate()`.

    A provider's `generate()` raising a connection error or timeout (httpx.HTTPError, the base
    class of httpx's connection/timeout exceptions), or a RuntimeError (both OllamaLLM and
    OpenAICompatLLM raise RuntimeError for an HTTP 5xx/429 response and for the empty-content
    case; see their own `generate()` methods) moves on to the next provider. Any other exception is
    treated as a bug rather than a transient provider failure and propagates immediately without
    trying the rest of the chain.

    Logs one INFO line naming which provider actually served the request on EVERY call, including
    the primary succeeding on the first attempt, plus one INFO line per provider that failed before
    that. Format (grepped for in services/orchestrator/tests/test_llm_fallback.py and in
    docs/reports/phase-8.md):
        llm.served_by provider=<name> model=<model_id> attempt=<n>
        llm.primary_failed provider=<name> error=<...>      (only the first provider's failure)
        llm.fallback_failed provider=<name> error=<...>      (any later provider's failure)
    """

    _FAILOVER_EXCEPTIONS = (httpx.HTTPError, RuntimeError)

    def __init__(self, providers: list[tuple[str, LLM]]):
        if not providers:
            raise ValueError("FallbackLLM needs at least one (name, LLM) provider.")
        self._providers = providers
        self._last_usage: dict | None = None

    @property
    def last_usage(self) -> dict | None:
        # Mirrors whichever inner provider actually served the most recent successful call -- see
        # each one's own last_usage for what it reports.
        return self._last_usage

    async def generate(self, system: str, user: str) -> str:
        last_exc: Exception | None = None
        for attempt, (name, llm) in enumerate(self._providers, start=1):
            try:
                result = await llm.generate(system, user)
            except self._FAILOVER_EXCEPTIONS as exc:
                event = "llm.primary_failed" if attempt == 1 else "llm.fallback_failed"
                logger.info("%s provider=%s error=%s", event, name, exc)
                last_exc = exc
                continue
            logger.info(
                "llm.served_by provider=%s model=%s attempt=%d",
                name,
                llm.model_id,
                attempt,
            )
            self._last_usage = llm.last_usage
            return result
        raise RuntimeError(
            f"All {len(self._providers)} LLM provider(s) in the fallback chain failed. "
            f"Last error: {last_exc}"
        ) from last_exc


_STUB_CONTEXT_RE = re.compile(r"^\[(\d+)\] Source:", re.MULTILINE)


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
    prompt strings -- in particular, never on eval/golden.jsonl's is_advice label.

    Cites a fixed subset of the retrieved context indices ([1], and [2] when a second context is
    present) in the `format_context` bracket numbering, so the citation-hallucination and
    unreferenced-citation checks have something real to parse. Citing only indices that are
    actually present in the prompt is what keeps citation_hallucination_rate at zero by
    construction; it is never coerced to zero after the fact.

    StubLLM used to have its own advice-detection branch (question-text patterns, refusing with a
    fixed string) so the CI invariant gate had a refusal path to exercise before the real guardrail
    existed. That branch is gone: the advice-vs-information decision now belongs entirely to
    app/guardrails/classifier.py, which runs before the generator is ever called (see
    app/pipeline.py) -- under LLM_PROVIDER=stub, Layer 1 (the deterministic rule) is the only layer
    that can fire, and StubLLM itself is never asked to make that decision at all, for either an
    advice-shaped or an informational question.
    """

    @property
    def model_id(self) -> str:
        return "stub"

    async def generate(self, system: str, user: str) -> str:
        del system  # unused: the stub's behavior depends only on the user prompt (see docstring)
        num_contexts = len(_STUB_CONTEXT_RE.findall(user))
        return _stub_answer_text(num_contexts)


class HostedLLM(LLM):
    """Seam for a hosted LLM API in production. Not wired up; superseded for Phase 8's real
    production path by OllamaLLM (is_cloud=True) as primary and OpenAICompatLLM as fallback, both
    above. Left in place as a dispatchable LLM_PROVIDER value for anything not yet using either.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key
        self._model = model

    @property
    def model_id(self) -> str:
        return self._model or "unknown-hosted"

    async def generate(self, system: str, user: str) -> str:
        raise NotImplementedError(
            "HostedLLM is a seam for a production LLM API and is not implemented yet. "
            "Set LLM_PROVIDER=ollama for local development."
        )


# =====================================================================================
# Model family resolution -- the generator/judge family guard (ARCHITECTURE.md: "The eval judge is
# a hosted model from a different family than the generator").
# =====================================================================================

# Checked in order; the FIRST pattern that matches wins. Order matters for exactly one real case in
# this project's configuration: NVIDIA's catalog lists `mistralai/mistral-nemotron`, a Nemotron
# model despite its "mistralai/" vendor prefix (it is a Mistral base model NVIDIA post-trained with
# their Nemotron recipe) -- so "nemotron" is checked before "mistral", and family comes from
# whichever keyword actually appears in the model name, never from the vendor prefix before the
# "/". Every model id currently reachable through this project's configuration (see
# resolve_generator_models below and eval/judge.py's JUDGE_MODEL) classifies here; a model id that
# matches none of these patterns raises rather than silently returning a placeholder family, because
# a placeholder that happens to differ from the judge's family would make the guard test in
# services/orchestrator/tests/test_model_family_guard.py pass for the wrong reason -- an unknown
# model must be a loud failure, never a quiet "different family".
_MODEL_FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("nemotron", re.compile(r"nemotron", re.IGNORECASE)),
    ("gpt-oss", re.compile(r"gpt-oss", re.IGNORECASE)),
    ("gemma", re.compile(r"gemma", re.IGNORECASE)),
    ("qwen", re.compile(r"qwen", re.IGNORECASE)),
    ("glm", re.compile(r"glm", re.IGNORECASE)),
    ("deepseek", re.compile(r"deepseek", re.IGNORECASE)),
    ("minimax", re.compile(r"minimax", re.IGNORECASE)),
    ("kimi", re.compile(r"kimi", re.IGNORECASE)),
    ("mistral", re.compile(r"mistral", re.IGNORECASE)),
    ("nomic", re.compile(r"nomic", re.IGNORECASE)),
)


def resolve_model_family(model_id: str) -> str:
    """Map a model id to a coarse "family" string, used to guard against the judge and a generator
    ever landing in the same family (self-preference bias -- see ARCHITECTURE.md).

    RAISES ValueError on a model id that matches none of the known families, rather than returning
    a value that would make the guard silently pass -- an unclassifiable model is a configuration
    failure to fix (add its family to _MODEL_FAMILY_PATTERNS above), never a "different family" the
    guard should wave through.
    """
    if not model_id or not model_id.strip():
        raise ValueError("Cannot classify model family for an empty model id.")
    for family, pattern in _MODEL_FAMILY_PATTERNS:
        if pattern.search(model_id):
            return family
    raise ValueError(
        f"Cannot classify model family for model id {model_id!r}. Add it to "
        "_MODEL_FAMILY_PATTERNS in app/providers/llm.py -- an unrecognized model must never "
        "silently pass the generator/judge family guard (services/orchestrator/tests/"
        "test_model_family_guard.py)."
    )


# =====================================================================================
# Provider dispatch / construction
# =====================================================================================


def _generator_chain_specs(settings: Settings) -> list[tuple[str, str, str]]:
    """(role, provider, model) for every generator the configuration resolves to, in failover
    order: index 0 is always the primary ("primary"), index 1 (if LLM_FALLBACK_PROVIDER is set) is
    the fallback ("fallback"). This is the single place both get_llm (to build real provider
    objects) and resolve_generator_models (read by the generator/judge family guard test) read
    from, so a config change is reflected in both without being duplicated.
    """
    chain = [("primary", settings.LLM_PROVIDER, settings.LLM_MODEL)]
    if settings.LLM_FALLBACK_PROVIDER:
        chain.append(("fallback", settings.LLM_FALLBACK_PROVIDER, settings.LLM_FALLBACK_MODEL))
    return chain


def resolve_generator_models(settings: Settings) -> list[str]:
    """Model ids for every generator the configuration resolves to (primary, then fallback if
    configured) -- derived from the same dispatch logic get_llm() itself uses, never a hardcoded
    list, so a future config change is caught by
    services/orchestrator/tests/test_model_family_guard.py instead of silently going unchecked.

    DELIBERATELY EXCLUDES Settings.CLASSIFIER_LLM_MODEL (Phase 8 round 3's cheap-model routing for
    the Layer 2 advice classifier, app/guardrails/classifier.py). See CLASSIFIER_LLM_PROVIDER's own
    comment in app/config.py for the full reasoning: the classifier's raw output never reaches the
    judge (only the generator's answer text is ever judged), so a family collision between the
    classifier and the judge cannot create the self-preference bias this guard exists to prevent.
    """
    return [model for _, _, model in _generator_chain_specs(settings)]


class ModelFamilyCollisionError(RuntimeError):
    """Raised when a generator in the RESOLVED failover chain (resolve_generator_models) shares
    resolve_model_family(settings.JUDGE_MODEL)'s family. ARCHITECTURE.md requires the eval judge to
    be a different family than every generator, to avoid self-preference bias -- a judge grading its
    own family's output silently corrupts faithfulness/answer_relevancy/comprehensibility, the
    metrics this whole project is built on. eval/run.py's main() calls
    assert_no_generator_judge_family_collision (below) as the very first thing it does, before a
    single judge call or generation call, so this state is unreachable rather than merely absent
    today -- see docs/adr's family-guard notes and services/orchestrator/tests/
    test_model_family_guard.py.
    """


def assert_no_generator_judge_family_collision(settings: Settings) -> None:
    """Raise ModelFamilyCollisionError if any generator in the resolved failover chain shares the
    judge's family; otherwise return None.

    Both sides are resolved from `settings` exactly as get_llm()/eval/judge.py would actually use
    them -- resolve_generator_models(settings) (the SAME dispatch function get_llm() itself calls,
    never a hardcoded "known bad" list) and resolve_model_family(settings.JUDGE_MODEL) -- so this
    catches a REAL misconfiguration (an LLM_FALLBACK_MODEL set to a same-family model in a real
    .env, in `fly secrets`, or in any other real environment `settings` was built from), not merely
    a fixture asserting against itself. `settings` is deliberately the caller's responsibility to
    resolve (normally get_settings(), which reads the real environment) rather than something this
    function re-derives, so the exact same object driving the rest of a run is what gets checked.
    """
    judge_family = resolve_model_family(settings.JUDGE_MODEL)
    collisions = [
        (model_id, resolve_model_family(model_id))
        for model_id in resolve_generator_models(settings)
    ]
    collisions = [(model_id, family) for model_id, family in collisions if family == judge_family]
    if collisions:
        collided = ", ".join(f"{model_id!r} (family {family!r})" for model_id, family in collisions)
        raise ModelFamilyCollisionError(
            f"Judge {settings.JUDGE_MODEL!r} (family {judge_family!r}) shares a model family with "
            f"{len(collisions)} generator(s) in the resolved failover chain: {collided}. "
            "ARCHITECTURE.md requires the eval judge to be a DIFFERENT family than every "
            "generator (self-preference bias would silently corrupt the eval gate's metrics). "
            "Fix LLM_MODEL, LLM_FALLBACK_MODEL, or JUDGE_MODEL before running eval/run.py again."
        )


def _build_provider(role: str, provider: str, model: str, settings: Settings) -> tuple[str, LLM]:
    """Build one (display_name, LLM) pair for the given chain slot. `role` is "primary" or
    "fallback" and selects which settings block (OLLAMA_* vs LLM_FALLBACK_*) supplies the base
    URL / API key for an "ollama"-shaped provider; display_name is what FallbackLLM logs and is
    deliberately decoupled from `provider` for "ollama" so a log line can distinguish local Ollama
    ("ollama") from Ollama Cloud ("ollama_cloud") even though LLM_PROVIDER is "ollama" either way.
    """
    if provider == "ollama":
        if role == "primary":
            base_url = settings.OLLAMA_BASE_URL
            api_key = settings.OLLAMA_API_KEY
            is_cloud = settings.OLLAMA_CLOUD
        else:
            base_url = settings.LLM_FALLBACK_BASE_URL
            api_key = settings.LLM_FALLBACK_API_KEY
            is_cloud = True
        llm = OllamaLLM(
            base_url=base_url,
            model=model,
            timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
            keep_alive=settings.OLLAMA_KEEP_ALIVE,
            think=settings.OLLAMA_THINK,
            api_key=api_key,
            is_cloud=is_cloud,
        )
        return ("ollama_cloud" if is_cloud else "ollama"), llm
    if provider == "hosted":
        return "hosted", HostedLLM(model=model)
    if provider == "stub":
        return "stub", StubLLM()
    # Any other identifier (e.g. "nvidia") is an OpenAI-compatible /v1/chat/completions endpoint,
    # wired up only as the fallback slot -- there is no OpenAI-compatible primary in this project's
    # configuration today, so a caller trying to set LLM_PROVIDER to one directly gets a clear
    # error instead of a confusing "LLM_FALLBACK_BASE_URL is empty" one.
    if role != "fallback":
        raise ValueError(
            f"LLM_PROVIDER={provider!r} is not a supported primary provider. The primary must be "
            "'ollama' or 'stub' (or the unimplemented 'hosted' seam); an OpenAI-compatible "
            "endpoint like NVIDIA is wired up only via LLM_FALLBACK_PROVIDER."
        )
    if not settings.LLM_FALLBACK_BASE_URL:
        raise ValueError(
            f"LLM_FALLBACK_PROVIDER={provider!r} is set but LLM_FALLBACK_BASE_URL is empty; it "
            "must be the full chat-completions URL (e.g. "
            "https://integrate.api.nvidia.com/v1/chat/completions)."
        )
    llm = OpenAICompatLLM(
        url=settings.LLM_FALLBACK_BASE_URL,
        model=model,
        api_key=settings.LLM_FALLBACK_API_KEY,
        timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
    )
    return provider, llm


def get_llm(settings: Settings) -> LLM:
    chain = _generator_chain_specs(settings)
    providers = [
        _build_provider(role, provider, model, settings) for role, provider, model in chain
    ]
    if len(providers) == 1:
        # No fallback configured (Settings.LLM_FALLBACK_PROVIDER == "", the default): return the
        # bare provider directly, exactly as get_llm always has -- a fresh local clone's behavior
        # is untouched by any of this.
        return providers[0][1]
    return FallbackLLM(providers)


def get_classifier_llm(settings: Settings) -> LLM | None:
    """Build the routed classifier model (Phase 8 round 3), or None when
    Settings.CLASSIFIER_LLM_PROVIDER is empty (the default) -- meaning "no routing configured", so
    app/pipeline.py falls back to the same generator LLM_MODEL for the Layer 2 advice classifier
    exactly as it always has. Built once at startup (app/main.py's lifespan), not per request, the
    same way get_llm/get_embedder already are.

    Reuses `_build_provider`'s "primary" slot (never LLM_FALLBACK_*'s base URL/API key) so a
    CLASSIFIER_LLM_PROVIDER="ollama" configuration talks to the SAME OLLAMA_BASE_URL/OLLAMA_API_KEY/
    OLLAMA_CLOUD this process already uses for the generator -- only the model tag differs. See
    resolve_generator_models's own comment for why this model is deliberately excluded from the
    generator/judge family guard.
    """
    if not settings.CLASSIFIER_LLM_PROVIDER:
        return None
    _, llm = _build_provider(
        "primary", settings.CLASSIFIER_LLM_PROVIDER, settings.CLASSIFIER_LLM_MODEL, settings
    )
    return llm
