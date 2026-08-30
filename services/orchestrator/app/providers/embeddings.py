"""Embedding provider interface.

Three implementations behind one interface: local Ollama for dev, a hosted API for production, and
a deterministic stub for the CI invariant gate (see eval/run.py's EVAL_MODE=ci / --ci and
docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md). The model name always comes from
Settings.EMBED_MODEL (app.config) for the real providers, never hardcoded here, so ingestion and
query can never drift onto two different embedding spaces.
"""

import hashlib
import math
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


class StubEmbedder(Embedder):
    """Deterministic, network-free stand-in for a real embedder, selected by EMBED_PROVIDER=stub.

    Same text always gives the same vector: each dimension is derived from repeated SHA-256
    hashing of the text (no randomness, no clock, no network call), then the vector is
    L2-normalized. This is a hash, not a semantic embedding -- it carries no notion of which texts
    are "similar", so retrieval results under this provider have no relationship to meaning. That
    is fine for the CI invariant gate (see eval/run.py's EVAL_MODE=ci / --ci): CI never measures
    answer quality, only that retrieval, citation mapping, and error handling behave correctly for
    whatever gets retrieved.
    """

    def __init__(self, dim: int):
        self._dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        seed = text.encode("utf-8")
        values: list[float] = []
        counter = 0
        while len(values) < self._dim:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for offset in range(0, len(digest), 8):
                if len(values) >= self._dim:
                    break
                chunk = digest[offset : offset + 8]
                as_unsigned = int.from_bytes(chunk, "big")
                # Map a uniform 64-bit unsigned int onto [-1, 1].
                values.append((as_unsigned / float(2**64 - 1)) * 2 - 1)
            counter += 1
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]


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
    if settings.EMBED_PROVIDER == "stub":
        return StubEmbedder(dim=settings.EMBED_DIM)
    raise ValueError(f"Unknown EMBED_PROVIDER: {settings.EMBED_PROVIDER!r}")
