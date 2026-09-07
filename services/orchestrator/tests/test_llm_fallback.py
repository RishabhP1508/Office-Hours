"""Phase 8: the production LLM failover chain (app/providers/llm.py::FallbackLLM, OllamaLLM's
Ollama-Cloud extension, and OpenAICompatLLM), plus get_llm()'s dispatch.

No network anywhere in this file: OllamaLLM/OpenAICompatLLM's httpx calls are exercised against a
fake httpx.AsyncClient (asserting on the exact request that would have gone out), and
FallbackLLM's ordering/logging is exercised against a small in-memory fake LLM, never the real
providers. This is what lets these tests assert real behavior (request shape, failover order, one
attempt per provider, log lines) without a reachable Ollama Cloud or NVIDIA endpoint.
"""

import logging

import httpx
import pytest

from app.config import Settings
from app.providers.llm import (
    LLM,
    FallbackLLM,
    HostedLLM,
    OllamaLLM,
    OpenAICompatLLM,
    StubLLM,
    get_llm,
    resolve_generator_models,
    resolve_model_family,
)

# =====================================================================================
# A fake httpx.AsyncClient, monkeypatched over app.providers.llm.httpx.AsyncClient so
# OllamaLLM/OpenAICompatLLM's request shape can be asserted on without a real network call.
# =====================================================================================


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or str(json_data)

    def json(self) -> dict:
        return self._json


class _FakeAsyncClient:
    """Records the single call made to .post() on `last_call` and returns `queued_response`."""

    queued_response: _FakeResponse
    last_call: dict | None = None

    def __init__(self, *, timeout=None):
        self._timeout = timeout

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_call = {"url": url, "json": json, "headers": headers or {}}
        return type(self).queued_response


@pytest.fixture
def fake_http(monkeypatch):
    """Installs _FakeAsyncClient in place of httpx.AsyncClient for app.providers.llm, and resets
    its recorded state before and after each test so tests never see a previous test's call.
    """
    import app.providers.llm as llm_module

    _FakeAsyncClient.last_call = None
    _FakeAsyncClient.queued_response = _FakeResponse(200, {"message": {"content": "ok"}})
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", _FakeAsyncClient)
    yield _FakeAsyncClient
    _FakeAsyncClient.last_call = None


# =====================================================================================
# OllamaLLM: local behavior unchanged, Ollama Cloud extension (api_key, is_cloud)
# =====================================================================================


async def test_ollama_llm_local_default_sends_keep_alive_and_think(fake_http):
    """The exact payload a fresh local clone has always sent -- is_cloud defaults to False, so
    this must be byte-for-byte unchanged by the Ollama Cloud work.
    """
    llm = OllamaLLM(
        base_url="http://host.docker.internal:11434",
        model="qwen3.5-8k:latest",
        timeout_seconds=600.0,
        keep_alive="60m",
        think=False,
    )
    result = await llm.generate("system", "user")
    assert result == "ok"
    sent = fake_http.last_call["json"]
    assert sent["keep_alive"] == "60m"
    assert sent["think"] is False
    assert fake_http.last_call["headers"] == {}  # no api_key configured -> no Authorization header


async def test_ollama_llm_local_default_sends_no_authorization_header_by_default(
    fake_http,
):
    llm = OllamaLLM(
        base_url="http://host.docker.internal:11434",
        model="qwen3.5-8k:latest",
        timeout_seconds=600.0,
        keep_alive="60m",
        think=False,
    )
    await llm.generate("system", "user")
    assert "Authorization" not in fake_http.last_call["headers"]


async def test_ollama_llm_sends_bearer_header_when_api_key_is_set(fake_http):
    llm = OllamaLLM(
        base_url="https://ollama.com",
        model="gpt-oss:120b",
        timeout_seconds=600.0,
        keep_alive="60m",
        think=False,
        api_key="secret-ollama-cloud-key",
    )
    await llm.generate("system", "user")
    assert fake_http.last_call["headers"]["Authorization"] == "Bearer secret-ollama-cloud-key"


async def test_ollama_llm_omits_keep_alive_and_think_when_is_cloud(fake_http):
    """keep_alive and think are local-Ollama-only concerns (ARCHITECTURE.md / app/config.py's own
    comments): Ollama Cloud may reject either field, so is_cloud=True must omit both rather than
    send local-only values to a cloud endpoint.
    """
    llm = OllamaLLM(
        base_url="https://ollama.com",
        model="gpt-oss:120b",
        timeout_seconds=600.0,
        keep_alive="60m",
        think=False,
        api_key="secret",
        is_cloud=True,
    )
    await llm.generate("system", "user")
    sent = fake_http.last_call["json"]
    assert "keep_alive" not in sent
    assert "think" not in sent
    # The rest of the request shape is unchanged.
    assert sent["model"] == "gpt-oss:120b"
    assert sent["stream"] is False


async def test_ollama_llm_model_id_property():
    assert OllamaLLM("http://x", "gpt-oss:120b", 1.0, "60m", False).model_id == "gpt-oss:120b"


async def test_ollama_llm_raises_runtime_error_on_non_200(fake_http):
    fake_http.queued_response = _FakeResponse(503, {}, text="service unavailable")
    llm = OllamaLLM("https://ollama.com", "gpt-oss:120b", 1.0, "60m", False)
    with pytest.raises(RuntimeError, match="503"):
        await llm.generate("s", "u")


async def test_ollama_llm_raises_runtime_error_on_empty_content(fake_http):
    fake_http.queued_response = _FakeResponse(200, {"message": {"content": "   "}})
    llm = OllamaLLM("https://ollama.com", "gpt-oss:120b", 1.0, "60m", False)
    with pytest.raises(RuntimeError, match="empty content"):
        await llm.generate("s", "u")


# --- Phase 8 round 4: real token usage, straight from Ollama's own response fields ---


async def test_ollama_llm_last_usage_is_none_before_any_call():
    llm = OllamaLLM("https://ollama.com", "gpt-oss:120b", 1.0, "60m", False)
    assert llm.last_usage is None


async def test_ollama_llm_last_usage_reports_real_counts_after_a_successful_call(fake_http):
    fake_http.queued_response = _FakeResponse(
        200,
        {"message": {"content": "ok"}, "prompt_eval_count": 123, "eval_count": 45},
    )
    llm = OllamaLLM("https://ollama.com", "gpt-oss:120b", 1.0, "60m", False)
    await llm.generate("s", "u")
    assert llm.last_usage == {"prompt_tokens": 123, "completion_tokens": 45}


async def test_ollama_llm_last_usage_reports_none_fields_when_ollama_omits_them(fake_http):
    fake_http.queued_response = _FakeResponse(200, {"message": {"content": "ok"}})
    llm = OllamaLLM("https://ollama.com", "gpt-oss:120b", 1.0, "60m", False)
    await llm.generate("s", "u")
    assert llm.last_usage == {"prompt_tokens": None, "completion_tokens": None}


# =====================================================================================
# OpenAICompatLLM: the NVIDIA /v1/chat/completions shape
# =====================================================================================


async def test_openai_compat_llm_parses_choices_content(fake_http):
    fake_http.queued_response = _FakeResponse(
        200, {"choices": [{"message": {"content": "the answer"}}]}
    )
    llm = OpenAICompatLLM(
        url="https://integrate.api.nvidia.com/v1/chat/completions",
        model="openai/gpt-oss-20b",
        api_key="nvidia-key",
        timeout_seconds=15.0,
    )
    result = await llm.generate("system", "user")
    assert result == "the answer"
    assert fake_http.last_call["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert fake_http.last_call["json"]["model"] == "openai/gpt-oss-20b"
    assert fake_http.last_call["headers"]["Authorization"] == "Bearer nvidia-key"


async def test_openai_compat_llm_omits_authorization_header_when_api_key_empty(
    fake_http,
):
    llm = OpenAICompatLLM(
        url="https://x/v1/chat/completions", model="m", api_key="", timeout_seconds=15.0
    )
    fake_http.queued_response = _FakeResponse(200, {"choices": [{"message": {"content": "x"}}]})
    await llm.generate("s", "u")
    assert "Authorization" not in fake_http.last_call["headers"]


async def test_openai_compat_llm_raises_on_non_200(fake_http):
    fake_http.queued_response = _FakeResponse(429, {}, text="rate limited")
    llm = OpenAICompatLLM(url="https://x", model="m", api_key="k", timeout_seconds=15.0)
    with pytest.raises(RuntimeError, match="429"):
        await llm.generate("s", "u")


async def test_openai_compat_llm_raises_on_empty_content(fake_http):
    fake_http.queued_response = _FakeResponse(200, {"choices": [{"message": {"content": ""}}]})
    llm = OpenAICompatLLM(url="https://x", model="m", api_key="k", timeout_seconds=15.0)
    with pytest.raises(RuntimeError, match="empty content"):
        await llm.generate("s", "u")


async def test_openai_compat_llm_raises_on_missing_choices(fake_http):
    fake_http.queued_response = _FakeResponse(200, {"choices": []})
    llm = OpenAICompatLLM(url="https://x", model="m", api_key="k", timeout_seconds=15.0)
    with pytest.raises(RuntimeError, match="empty content"):
        await llm.generate("s", "u")


# --- Phase 8 round 4: real token usage, straight from the OpenAI-compatible `usage` block ---


async def test_openai_compat_llm_last_usage_reports_real_counts_from_the_usage_block(fake_http):
    fake_http.queued_response = _FakeResponse(
        200,
        {
            "choices": [{"message": {"content": "the answer"}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230},
        },
    )
    llm = OpenAICompatLLM(url="https://x", model="m", api_key="k", timeout_seconds=15.0)
    await llm.generate("s", "u")
    assert llm.last_usage == {"prompt_tokens": 200, "completion_tokens": 30}


async def test_openai_compat_llm_last_usage_reports_none_fields_when_usage_is_absent(fake_http):
    fake_http.queued_response = _FakeResponse(
        200, {"choices": [{"message": {"content": "the answer"}}]}
    )
    llm = OpenAICompatLLM(url="https://x", model="m", api_key="k", timeout_seconds=15.0)
    await llm.generate("s", "u")
    assert llm.last_usage == {"prompt_tokens": None, "completion_tokens": None}


# =====================================================================================
# FallbackLLM: ordering, one-attempt-per-provider, and the required log lines
# =====================================================================================


class _FakeLLM(LLM):
    """In-memory LLM stand-in: `outcomes` is a list consumed one per call, either a string
    (success, returned as-is) or an exception instance (raised). Counts calls so tests can assert
    a provider is tried AT MOST once (no retry-looping within a single provider). `usage`
    (Phase 8 round 4) is this fake's own `last_usage`, set unconditionally regardless of whether
    the most recent call succeeded -- fine for these tests, which only ever read it after a
    successful call.
    """

    def __init__(self, model_id: str, outcomes: list, usage: dict | None = None):
        self._model_id = model_id
        self._outcomes = list(outcomes)
        self.calls = 0
        self._usage = usage

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def last_usage(self) -> dict | None:
        return self._usage

    async def generate(self, system: str, user: str) -> str:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def test_fallback_llm_serves_primary_on_success_and_logs_served_by(caplog):
    caplog.set_level(logging.INFO, logger="app.providers.llm")
    primary = _FakeLLM("gpt-oss:120b", ["primary answer"])
    fallback = _FakeLLM("openai/gpt-oss-20b", ["fallback answer"])
    llm = FallbackLLM([("ollama_cloud", primary), ("nvidia", fallback)])

    result = await llm.generate("s", "u")

    assert result == "primary answer"
    assert primary.calls == 1
    assert fallback.calls == 0  # never tried: primary succeeded
    assert "llm.served_by provider=ollama_cloud model=gpt-oss:120b attempt=1" in caplog.text


async def test_fallback_llm_last_usage_mirrors_the_provider_that_actually_served_the_request():
    primary = _FakeLLM("gpt-oss:120b", [RuntimeError("boom")], usage={"prompt_tokens": 1})
    fallback = _FakeLLM(
        "openai/gpt-oss-20b",
        ["fallback answer"],
        usage={"prompt_tokens": 200, "completion_tokens": 30},
    )
    llm = FallbackLLM([("ollama_cloud", primary), ("nvidia", fallback)])

    assert llm.last_usage is None  # nothing served yet
    await llm.generate("s", "u")

    assert llm.last_usage == {"prompt_tokens": 200, "completion_tokens": 30}


async def test_fallback_llm_fails_over_and_logs_both_lines(caplog):
    caplog.set_level(logging.INFO, logger="app.providers.llm")
    primary = _FakeLLM("gpt-oss:120b", [RuntimeError("Ollama /api/chat returned HTTP 503: boom")])
    fallback = _FakeLLM("openai/gpt-oss-20b", ["fallback answer"])
    llm = FallbackLLM([("ollama_cloud", primary), ("nvidia", fallback)])

    result = await llm.generate("s", "u")

    assert result == "fallback answer"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert "llm.primary_failed provider=ollama_cloud error=" in caplog.text
    assert "llm.served_by provider=nvidia model=openai/gpt-oss-20b attempt=2" in caplog.text


async def test_fallback_llm_tries_each_provider_at_most_once(caplog):
    """Failover, not retry: a provider that fails is never called a second time within the same
    generate() call, even though it is the only provider left in the outcomes list to consume.
    """
    caplog.set_level(logging.INFO, logger="app.providers.llm")
    primary = _FakeLLM("p", [httpx.ConnectError("connection refused")])
    fallback = _FakeLLM("f", ["ok"])
    llm = FallbackLLM([("primary", primary), ("fallback", fallback)])

    await llm.generate("s", "u")

    assert primary.calls == 1
    assert fallback.calls == 1


async def test_fallback_llm_raises_after_every_provider_fails():
    primary = _FakeLLM("p", [RuntimeError("primary down")])
    fallback = _FakeLLM("f", [RuntimeError("fallback down")])
    llm = FallbackLLM([("primary", primary), ("fallback", fallback)])

    with pytest.raises(RuntimeError, match="fallback down"):
        await llm.generate("s", "u")

    assert primary.calls == 1
    assert fallback.calls == 1


async def test_fallback_llm_triggers_on_httpx_connect_error_and_timeout():
    for exc in (httpx.ConnectError("refused"), httpx.TimeoutException("timed out")):
        primary = _FakeLLM("p", [exc])
        fallback = _FakeLLM("f", ["ok"])
        llm = FallbackLLM([("primary", primary), ("fallback", fallback)])
        result = await llm.generate("s", "u")
        assert result == "ok"


async def test_fallback_llm_does_not_catch_unrelated_exceptions():
    """A bug (e.g. a TypeError from malformed calling code) is not a transient provider failure
    and must propagate immediately, never triggering failover to the next provider.
    """
    primary = _FakeLLM("p", [TypeError("not a provider failure")])
    fallback = _FakeLLM("f", ["ok"])
    llm = FallbackLLM([("primary", primary), ("fallback", fallback)])

    with pytest.raises(TypeError):
        await llm.generate("s", "u")
    assert fallback.calls == 0


def test_fallback_llm_requires_at_least_one_provider():
    with pytest.raises(ValueError):
        FallbackLLM([])


# =====================================================================================
# get_llm() dispatch: local dev unchanged, production wires FallbackLLM
# =====================================================================================


def test_get_llm_local_default_returns_bare_ollama_llm_not_wrapped():
    """LLM_PROVIDER="ollama" with LLM_FALLBACK_PROVIDER="" is the local dev default (both are
    app/config.py::Settings' own code defaults); get_llm must return the OllamaLLM directly, never
    a FallbackLLM of one, so a fresh clone's behavior (including object identity assumptions
    elsewhere) is completely unchanged.

    Both are pinned explicitly here rather than read off a bare `Settings()`: pydantic-settings
    reads the ambient OS environment, and .github/workflows/eval.yml's ci-invariant-gate job sets
    LLM_PROVIDER=stub in its own environment for the orchestrator it stands up -- a bare
    `Settings()` constructed in THIS test's process (a separate `pytest` invocation, but the job
    still exports that env var into every step, including `pytest -m "not full_corpus" -v`) would
    pick that up and make `isinstance(result, OllamaLLM)` false there, which is exactly what
    happened. This test's
    point is the CODE default, not whatever the process's ambient environment happens to hold.
    """
    settings = Settings(LLM_PROVIDER="ollama", LLM_FALLBACK_PROVIDER="")
    result = get_llm(settings)
    assert isinstance(result, OllamaLLM)
    assert not isinstance(result, FallbackLLM)
    assert result.model_id == settings.LLM_MODEL


def test_get_llm_dispatches_stub_provider_still_bare():
    assert isinstance(get_llm(Settings(LLM_PROVIDER="stub")), StubLLM)


def test_get_llm_wires_a_fallback_chain_when_configured():
    settings = Settings(
        LLM_PROVIDER="ollama",
        LLM_MODEL="gpt-oss:120b",
        OLLAMA_BASE_URL="https://ollama.com",
        OLLAMA_API_KEY="k1",
        OLLAMA_CLOUD=True,
        LLM_FALLBACK_PROVIDER="nvidia",
        LLM_FALLBACK_MODEL="openai/gpt-oss-20b",
        LLM_FALLBACK_BASE_URL="https://integrate.api.nvidia.com/v1/chat/completions",
        LLM_FALLBACK_API_KEY="k2",
    )
    result = get_llm(settings)
    assert isinstance(result, FallbackLLM)
    names = [name for name, _ in result._providers]
    assert names == ["ollama_cloud", "nvidia"]
    models = [provider.model_id for _, provider in result._providers]
    assert models == ["gpt-oss:120b", "openai/gpt-oss-20b"]
    assert isinstance(result._providers[0][1], OllamaLLM)
    assert isinstance(result._providers[1][1], OpenAICompatLLM)


def test_get_llm_rejects_an_unsupported_primary_provider():
    with pytest.raises(ValueError):
        get_llm(Settings(LLM_PROVIDER="nvidia"))  # only valid as a fallback, never as primary


def test_get_llm_rejects_a_fallback_provider_with_no_base_url():
    with pytest.raises(ValueError):
        get_llm(Settings(LLM_PROVIDER="ollama", LLM_FALLBACK_PROVIDER="nvidia"))


def test_hosted_llm_model_id_falls_back_to_placeholder():
    assert HostedLLM().model_id == "unknown-hosted"
    assert HostedLLM(model="x").model_id == "x"


# =====================================================================================
# resolve_generator_models: the single source both get_llm and the family guard test read
# =====================================================================================


def test_resolve_generator_models_returns_only_primary_with_no_fallback_configured():
    assert resolve_generator_models(Settings()) == [Settings().LLM_MODEL]


def test_resolve_generator_models_returns_primary_then_fallback_when_configured():
    settings = Settings(
        LLM_MODEL="gpt-oss:120b",
        LLM_FALLBACK_PROVIDER="nvidia",
        LLM_FALLBACK_MODEL="openai/gpt-oss-20b",
        LLM_FALLBACK_BASE_URL="https://x",
    )
    assert resolve_generator_models(settings) == ["gpt-oss:120b", "openai/gpt-oss-20b"]


# =====================================================================================
# resolve_model_family: sanity on the real model ids in play (the guard test in
# test_model_family_guard.py exercises the family-collision logic itself)
# =====================================================================================


@pytest.mark.parametrize(
    "model_id,expected_family",
    [
        ("gpt-oss:120b", "gpt-oss"),
        ("openai/gpt-oss-20b", "gpt-oss"),
        ("nvidia/nemotron-3.5-lightning-30b-a3b", "nemotron"),
        (
            "mistralai/mistral-nemotron",
            "nemotron",
        ),  # NOT "mistral" -- see llm.py's own comment
        ("google/gemma-4-31b-it", "gemma"),
        ("qwen3.5-8k:latest", "qwen"),
        ("nomic-embed-text", "nomic"),
        ("mistralai/mistral-large-2-instruct", "mistral"),
    ],
)
def test_resolve_model_family_known_shapes(model_id, expected_family):
    assert resolve_model_family(model_id) == expected_family


def test_resolve_model_family_raises_on_unknown_model():
    with pytest.raises(ValueError):
        resolve_model_family("some-vendor/a-model-nobody-has-ever-heard-of")


def test_resolve_model_family_raises_on_empty_string():
    with pytest.raises(ValueError):
        resolve_model_family("")
