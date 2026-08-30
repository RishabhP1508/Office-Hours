"""LLM provider interface.

Two implementations behind one interface: local Ollama for dev, a hosted API for production.
"""

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
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.LLM_PROVIDER!r}")
