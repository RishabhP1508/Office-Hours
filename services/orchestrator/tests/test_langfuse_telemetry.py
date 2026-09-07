"""Langfuse wiring tests (app/langfuse_telemetry.py, Phase 8 round 4).

No real Langfuse project or network call anywhere in this file: `langfuse.Langfuse` itself is
monkeypatched with a small in-memory fake that records exactly what it was called with, the same
convention tests/test_llm_fallback.py uses for httpx.AsyncClient. This is what lets these tests
assert the real shape of the SDK call this project makes without a live LANGFUSE_PUBLIC_KEY/
LANGFUSE_SECRET_KEY (which do not exist yet -- see app/langfuse_telemetry.py's own docstring,
"NOT YET VERIFIED AGAINST A REAL LANGFUSE PROJECT").
"""

from __future__ import annotations

import pytest

import app.langfuse_telemetry as langfuse_telemetry
from app.config import Settings


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Every test starts and ends with the module-level client reset to None, so tests never leak
    state into each other regardless of execution order."""
    langfuse_telemetry._client = None
    yield
    langfuse_telemetry._client = None


class _FakeGenerationHandle:
    """What `_client.start_as_current_generation(...)` returns, used as a context manager -- see
    this class's own `update` for what gets recorded.
    """

    def __init__(self, recorder: dict):
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def update(self, **kwargs):
        self._recorder["update_kwargs"] = kwargs


class _FakeLangfuseClient:
    """Records the exact kwargs `start_as_current_generation` and the constructor were called
    with, on the class itself, so a test can construct via the real `Langfuse(...)` call site in
    app/langfuse_telemetry.py and still inspect what happened afterward.
    """

    last_init_kwargs: dict | None = None
    last_generation_kwargs: dict | None = None
    last_update_kwargs: dict | None = None
    raise_on_start: Exception | None = None

    def __init__(self, **kwargs):
        type(self).last_init_kwargs = kwargs

    def start_as_current_generation(self, **kwargs):
        type(self).last_generation_kwargs = kwargs
        if type(self).raise_on_start is not None:
            raise type(self).raise_on_start
        recorder: dict = {}
        handle = _FakeGenerationHandle(recorder)
        # Stash the recorder on the class so the test can read update_kwargs after the `with`
        # block exits (the real generation object is discarded by then).
        type(self)._last_recorder = recorder
        return handle

    @classmethod
    def read_update_kwargs(cls) -> dict | None:
        recorder = getattr(cls, "_last_recorder", None)
        return recorder.get("update_kwargs") if recorder else None


@pytest.fixture(autouse=True)
def _reset_fake_client():
    _FakeLangfuseClient.last_init_kwargs = None
    _FakeLangfuseClient.last_generation_kwargs = None
    _FakeLangfuseClient.raise_on_start = None
    yield


# =================================================================================================
# setup_langfuse: off by default, on only when BOTH keys are set
# =================================================================================================


def test_disabled_by_default_with_no_keys_configured():
    langfuse_telemetry.setup_langfuse(Settings())
    assert langfuse_telemetry.is_enabled() is False


def test_disabled_when_only_the_public_key_is_set():
    langfuse_telemetry.setup_langfuse(Settings(LANGFUSE_PUBLIC_KEY="pk-lf-x"))
    assert langfuse_telemetry.is_enabled() is False


def test_disabled_when_only_the_secret_key_is_set():
    langfuse_telemetry.setup_langfuse(Settings(LANGFUSE_SECRET_KEY="sk-lf-x"))
    assert langfuse_telemetry.is_enabled() is False


def test_enabled_and_constructs_a_client_when_both_keys_are_set(monkeypatch):
    monkeypatch.setattr("langfuse.Langfuse", _FakeLangfuseClient)
    langfuse_telemetry.setup_langfuse(
        Settings(
            LANGFUSE_PUBLIC_KEY="pk-lf-x",
            LANGFUSE_SECRET_KEY="sk-lf-y",
            LANGFUSE_HOST="https://us.cloud.langfuse.com",
        )
    )
    assert langfuse_telemetry.is_enabled() is True
    assert _FakeLangfuseClient.last_init_kwargs == {
        "public_key": "pk-lf-x",
        "secret_key": "sk-lf-y",
        "host": "https://us.cloud.langfuse.com",
    }


def test_setup_never_raises_when_the_client_constructor_fails(monkeypatch):
    class _ExplodingClient:
        def __init__(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr("langfuse.Langfuse", _ExplodingClient)
    langfuse_telemetry.setup_langfuse(
        Settings(LANGFUSE_PUBLIC_KEY="pk-lf-x", LANGFUSE_SECRET_KEY="sk-lf-y")
    )
    assert langfuse_telemetry.is_enabled() is False


# =================================================================================================
# record_generation_trace: no-op when disabled; real shape when enabled; never raises
# =================================================================================================


def test_record_generation_trace_is_a_noop_when_disabled():
    # Disabled (default fixture state) -- must not raise, regardless of arguments.
    langfuse_telemetry.record_generation_trace(
        name="generate",
        model_id="gpt-oss:120b",
        prompt_version="abc123",
        response_type="answer",
        is_advice=False,
        user_prompt="a prompt",
        answer_text="an answer",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )


def test_record_generation_trace_records_the_expected_shape_when_enabled(monkeypatch):
    monkeypatch.setattr("langfuse.Langfuse", _FakeLangfuseClient)
    langfuse_telemetry.setup_langfuse(
        Settings(LANGFUSE_PUBLIC_KEY="pk-lf-x", LANGFUSE_SECRET_KEY="sk-lf-y")
    )

    langfuse_telemetry.record_generation_trace(
        name="generate",
        model_id="gpt-oss:120b",
        prompt_version="abc123def456",
        response_type="refusal_advice",
        is_advice=True,
        user_prompt="Context passages:\n...\n\nQuestion: Should I switch jobs?",
        answer_text="State the rule... talk to your DSO.",
        usage={"prompt_tokens": 500, "completion_tokens": 80},
    )

    generation_kwargs = _FakeLangfuseClient.last_generation_kwargs
    assert generation_kwargs["name"] == "generate"
    assert generation_kwargs["model"] == "gpt-oss:120b"
    assert generation_kwargs["input"] == (
        "Context passages:\n...\n\nQuestion: Should I switch jobs?"
    )
    assert generation_kwargs["metadata"] == {
        "prompt_version": "abc123def456",
        "response_type": "refusal_advice",
        "is_advice": True,
    }

    update_kwargs = _FakeLangfuseClient.read_update_kwargs()
    assert update_kwargs["output"] == "State the rule... talk to your DSO."
    assert update_kwargs["usage_details"] == {"input": 500, "output": 80}


def test_record_generation_trace_omits_usage_details_when_usage_is_none(monkeypatch):
    monkeypatch.setattr("langfuse.Langfuse", _FakeLangfuseClient)
    langfuse_telemetry.setup_langfuse(
        Settings(LANGFUSE_PUBLIC_KEY="pk-lf-x", LANGFUSE_SECRET_KEY="sk-lf-y")
    )

    langfuse_telemetry.record_generation_trace(
        name="generate",
        model_id="stub",
        prompt_version="abc123",
        response_type="answer",
        is_advice=False,
        user_prompt="a prompt",
        answer_text="an answer",
        usage=None,
    )

    update_kwargs = _FakeLangfuseClient.read_update_kwargs()
    assert update_kwargs["usage_details"] is None


def test_record_generation_trace_never_raises_when_the_sdk_call_itself_fails(monkeypatch):
    monkeypatch.setattr("langfuse.Langfuse", _FakeLangfuseClient)
    langfuse_telemetry.setup_langfuse(
        Settings(LANGFUSE_PUBLIC_KEY="pk-lf-x", LANGFUSE_SECRET_KEY="sk-lf-y")
    )
    _FakeLangfuseClient.raise_on_start = RuntimeError("Langfuse is unreachable")

    # Must not raise -- this is the core safety property (see app/langfuse_telemetry.py's own
    # docstring, "NEVER BREAKS A REQUEST").
    langfuse_telemetry.record_generation_trace(
        name="generate",
        model_id="gpt-oss:120b",
        prompt_version="abc123",
        response_type="answer",
        is_advice=False,
        user_prompt="a prompt",
        answer_text="an answer",
        usage=None,
    )
