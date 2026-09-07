"""Embedding provider interface.

Implementations behind one interface: local Ollama for dev, an in-process GGUF embedder for
production (GGUFEmbedder, Phase 8 round 8, docs/adr/0013-in-process-gguf-embeddings.md), and a
deterministic stub for the CI invariant gate (see eval/run.py's EVAL_MODE=ci / --ci and
docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md). The model name always comes from
Settings.EMBED_MODEL (app.config) for the real providers, never hardcoded here, so ingestion and
query can never drift onto two different embedding spaces.
"""

import asyncio
import hashlib
import logging
import math
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


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


class GGUFEmbedder(Embedder):
    """Loads nomic-embed-text's own GGUF file -- the exact one Ollama itself serves -- in-process
    via llama-cpp-python, selected by EMBED_PROVIDER=gguf. This is production's real embedding
    provider (Phase 8 round 8): no API, no vendor, no third machine. Neither Ollama Cloud nor
    NVIDIA serves nomic-embed-text, and the 221 vectors already stored in `documents` were built by
    real local Ollama, so the only way to keep querying in the SAME embedding space without
    re-embedding the corpus is to run the identical weights, the identical engine (llama.cpp is
    what Ollama itself uses under the hood), and -- the one non-obvious part, see point 2 below --
    the identical batch configuration. See docs/adr/0013-in-process-gguf-embeddings.md for the full
    measurement.

    Two facts make this safe rather than merely plausible, both measured directly against this
    project's real corpus and a real local Ollama, never assumed:

    1. RAW TEXT, NO INSTRUCTION PREFIX, EVER. Ollama's own Modelfile for nomic-embed-text is
       `TEMPLATE {{ .Prompt }}` -- no "search_query:"/"search_document:" prefix at all, even though
       nomic-embed-text's model card documents those as optional task prefixes. Measured against 3
       probe strings, against real local Ollama output: raw text matches at cosine
       0.99999980/0.99999715/0.99999980; adding "search_query: " drops that to
       0.970/0.982/0.976, and "search_document: " to 0.909/0.969/0.859. So `embed()` below sends
       every input completely unchanged. DO NOT add a task-instruction prefix here to "follow the
       model card" -- it would silently invalidate every one of the 221 vectors already stored in
       `documents`, and nothing downstream would catch it: cosine distance would just get uniformly
       worse everywhere, which reads as "retrieval got a little weaker," not "the embedder broke."

    2. n_batch = n_ubatch = n_ctx, ALWAYS -- not merely "the same weights in the same engine".
       llama.cpp's own default, n_batch=512, silently splits any longer input into multiple batches
       and returns a genuinely DIFFERENT vector for it: measured directly against 25 real stored
       corpus vectors, n_batch=512 gave 5 of 25 below cosine 0.9999 (min 0.974), and every one of
       those 5 was a chunk of >=583 tokens (every chunk <=485 tokens matched). Setting
       n_batch=n_ubatch=n_ctx gave 0 of 25 below 0.9999 (min 0.99999412). This class makes the three
       settings impossible to disagree: the constructor takes ONE `n_ctx` argument and derives
       n_batch/n_ubatch from it, rather than exposing three settings a future .env edit could set
       inconsistently.
    """

    def __init__(self, model_path: str, n_ctx: int, n_threads: int):
        if not model_path:
            raise RuntimeError(
                "EMBED_PROVIDER=gguf requires EMBED_GGUF_PATH to be set to the nomic-embed-text "
                "GGUF file's path. Production bakes this file into the image at build time (see "
                "services/orchestrator/Dockerfile and the README's deploy guide for how to obtain "
                "it from Ollama's own local blob store)."
            )
        if not Path(model_path).is_file():
            raise RuntimeError(
                f"EMBED_GGUF_PATH={model_path!r} does not exist on disk. Production bakes the "
                "GGUF file into the image at build time; local dev can point this at the file "
                "`ollama show nomic-embed-text --modelfile` names under Ollama's own blob store."
            )
        try:
            from llama_cpp import Llama
        except ImportError as exc:  # pragma: no cover - exercised only when the extra is absent
            raise RuntimeError(
                "EMBED_PROVIDER=gguf requires the llama-cpp-python package, which is not "
                "installed in this image. See services/orchestrator/pyproject.toml's `gguf` "
                "extra and Dockerfile's INSTALL_EXTRAS."
            ) from exc
        # Loaded ONCE, here, at process startup -- app/main.py's lifespan calls
        # get_embedder(settings) exactly once, the same way it already does for
        # get_llm/get_classifier_llm. Constructing a Llama object parses and mmaps the whole
        # ~262MB GGUF file; doing that per request would pay that cost on every single /query call
        # instead of once at boot.
        self._llama = Llama(
            model_path=model_path,
            embedding=True,
            n_ctx=n_ctx,
            n_batch=n_ctx,
            n_ubatch=n_ctx,
            n_threads=n_threads,
            verbose=False,
        )
        self._n_ctx = n_ctx
        logger.info(
            "gguf_embedder.loaded model_path=%s n_ctx=n_batch=n_ubatch=%d n_threads=%d",
            model_path,
            n_ctx,
            n_threads,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # llama-cpp-python's embedding call is synchronous and CPU-bound; running it directly here
        # would block the event loop (and every other in-flight request) for the call's duration, so
        # it runs in a worker thread instead.
        return await asyncio.to_thread(self._embed_sync, texts)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        for text in texts:
            self._raise_if_over_budget(text)
        # truncate=False (never the library's own default of True): a truncated input still
        # returns a success-shaped vector for the WRONG text, indistinguishable downstream from a
        # vector for the right one. _raise_if_over_budget above already raises first with a
        # clearer message naming the exact token count and limit; this is the backstop in case
        # that check and llama.cpp's own tokenization ever disagree by an edge case.
        # `texts` is always passed as a list (never a bare str): llama_cpp.Llama.embed() only
        # unwraps its return value to a single flat vector when its OWN input is a bare str, and
        # returns one embedding per element, in input order, whenever its input is a list -- even a
        # list of one. Passing a list here always, never a str, is what keeps this return shape
        # `list[list[float]]` regardless of how many texts were requested.
        return self._llama.embed(texts, truncate=False)

    def _raise_if_over_budget(self, text: str) -> None:
        # add_bos=True, special=False: the exact tokenization llama_cpp.Llama.embed() itself uses
        # internally to decide whether to split/truncate, so this check's token count is the same
        # count that would actually be sent to the model, never an approximation of it.
        token_count = len(self._llama.tokenize(text.encode("utf-8"), add_bos=True, special=False))
        if token_count > self._n_ctx:
            raise ValueError(
                f"Input is {token_count} tokens, over the {self._n_ctx}-token GGUF context "
                "budget (EMBED_GGUF_N_CTX). Truncating would silently embed the wrong text -- a "
                "vector that looks perfectly valid downstream but corresponds to only part of "
                f"what was asked to be embedded -- so this raises instead. Text starts: "
                f"{text[:80]!r}"
            )


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
    """Seam for a hosted embedding API. Never wired up: neither Ollama Cloud nor NVIDIA serves
    nomic-embed-text, and re-embedding the corpus on a different model was rejected (it would move
    the already knife-edge NO_ANSWER_MAX_DISTANCE threshold with no way to re-derive it safely --
    see docs/adr/0013-in-process-gguf-embeddings.md). Production uses GGUFEmbedder instead."""

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
    if settings.EMBED_PROVIDER == "gguf":
        return GGUFEmbedder(
            model_path=settings.EMBED_GGUF_PATH,
            n_ctx=settings.EMBED_GGUF_N_CTX,
            n_threads=settings.EMBED_GGUF_THREADS,
        )
    if settings.EMBED_PROVIDER == "hosted":
        return HostedEmbedder(model=settings.EMBED_MODEL)
    if settings.EMBED_PROVIDER == "stub":
        return StubEmbedder(dim=settings.EMBED_DIM)
    raise ValueError(f"Unknown EMBED_PROVIDER: {settings.EMBED_PROVIDER!r}")
