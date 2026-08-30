"""Embedding provider interface.

Two implementations behind one interface: local Ollama for dev, a hosted API for production. The
model name always comes from Settings.EMBED_MODEL (app.config), never hardcoded here, so ingestion
and query can never drift onto two different embedding spaces.
"""

from abc import ABC, abstractmethod

import httpx

from app.config import Settings


class Embedder(ABC):
    """Turns text into vectors. The corpus and the query must use the same implementation/model."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text, in the same order."""
        raise NotImplementedError


class OllamaEmbedder(Embedder):
    def __init__(self, base_url: str, model: str, timeout_seconds: float = 180.0):
        self._base_url = base_url.rstrip("/")
        self._model = model
        # Ollama can take a long time to answer on a cold model load, so the read timeout comes
        # from Settings.EMBED_TIMEOUT_SECONDS rather than httpx's 5s default. Connect timeout
        # stays short: a dead/unreachable host should fail fast.
        self._timeout = httpx.Timeout(timeout_seconds, connect=10.0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/api/embed",
                json={"model": self._model, "input": texts},
            )
            if response.status_code != 200:
                # Not response.raise_for_status(): see the identical note in providers/llm.py.
                # The response body is where Ollama's real failure reason lives, and it must
                # survive into whatever catches this exception.
                raise RuntimeError(
                    f"Ollama /api/embed returned HTTP {response.status_code} for model "
                    f"{self._model!r}: {response.text}"
                )
            data = response.json()
        embeddings = data.get("embeddings")
        # `texts` is non-empty past the guard above, so a missing/empty embeddings list, or an
        # individual empty vector inside it, is a real failure -- not a legitimate "nothing to
        # embed" case -- and must not be handed to the caller as if it were a usable vector.
        if not embeddings:
            raise RuntimeError(f"Ollama /api/embed returned no 'embeddings' field: {data}")
        if any(not vector for vector in embeddings):
            raise RuntimeError(f"Ollama /api/embed returned an empty embedding vector: {data}")
        return embeddings


class HostedEmbedder(Embedder):
    """Seam for a hosted embedding API in production. Not wired up in Phase 0."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(
            "HostedEmbedder is a seam for a production embedding API and is not implemented yet. "
            "Set EMBED_PROVIDER=ollama for local development."
        )


def get_embedder(settings: Settings) -> Embedder:
    if settings.EMBED_PROVIDER == "ollama":
        return OllamaEmbedder(
            base_url=settings.OLLAMA_BASE_URL,
            model=settings.EMBED_MODEL,
            timeout_seconds=settings.EMBED_TIMEOUT_SECONDS,
        )
    if settings.EMBED_PROVIDER == "hosted":
        return HostedEmbedder(model=settings.EMBED_MODEL)
    raise ValueError(f"Unknown EMBED_PROVIDER: {settings.EMBED_PROVIDER!r}")
