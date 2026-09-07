"""GGUFEmbedder equivalence test (Phase 8 round 8, app/providers/embeddings.py::GGUFEmbedder).

Guards the two facts docs/adr/0013-in-process-gguf-embeddings.md and GGUFEmbedder's own docstring
depend on: this in-process embedder must reproduce nomic-embed-text's real, already-stored vectors
closely enough that Postgres's HNSW index still finds the right chunks for a query embedded this
way. Compares against REAL stored vectors read straight from `documents` -- never a fixture, never a
hand-constructed vector -- spanning both short and long chunks: the batch-splitting divergence this
guards against (GGUFEmbedder's own docstring, point 2) only appears past roughly 512 tokens, so a
short-only sample would pass with that regression present.

Skipped cleanly wherever the GGUF file or the llama-cpp-python package is not available -- neither
is installed by the CI invariant gate (.github/workflows/eval.yml's `pull_request` job installs
`[dev,eval-ci]`, never the `[gguf]` extra), following the same convention test_cache.py's
`requires_real_ollama_embedder` uses for a real-Ollama dependency.

DB access here is READ-ONLY (a plain SELECT from `documents`, never a write), so this may run
against the live, fully-ingested corpus -- the same convention test_freshness.py documents for
read-only tests (only DB-WRITING tests need to refuse the real corpus).
"""

from __future__ import annotations

import importlib.util
import math
import os
import re
from pathlib import Path

import psycopg
import pytest

from app.config import get_settings
from app.providers.embeddings import GGUFEmbedder

# How many real stored chunks the equivalence test samples, spread across the corpus's full
# short-to-long length distribution (see _sample_indices below) -- not just the extremes, and not
# just the median. 25 matches the sample size this project's own real measurement
# (docs/adr/0013-in-process-gguf-embeddings.md) was taken at.
_SAMPLE_SIZE = 25

# The contract this whole class exists to keep (GGUFEmbedder's own docstring, point 2): every
# sampled chunk's freshly-computed vector must match its already-stored one at least this closely.
_MIN_COSINE = 0.9999


def _gguf_path() -> str:
    return os.environ.get("EMBED_GGUF_PATH", get_settings().EMBED_GGUF_PATH)


def _gguf_available() -> bool:
    if importlib.util.find_spec("llama_cpp") is None:
        return False
    path = _gguf_path()
    return bool(path) and Path(path).is_file()


requires_gguf_model = pytest.mark.skipif(
    not _gguf_available(),
    reason=(
        "requires the llama-cpp-python package AND a real nomic-embed-text GGUF file at "
        "EMBED_GGUF_PATH -- neither is installed/present in CI's ci-invariant-gate job; run this "
        "locally with the orchestrator's `gguf` extra installed and EMBED_GGUF_PATH pointed at the "
        "file `ollama show nomic-embed-text --modelfile` names under Ollama's own blob store"
    ),
)


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", get_settings().DATABASE_URL)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b)


def _parse_stored_vector(raw: str) -> list[float]:
    # psycopg returns a pgvector column as its literal text form ("[0.1,0.2,...]") unless
    # pgvector.psycopg.register_vector[_async] has been called on the connection -- this test never
    # registers it (there is nothing to insert or bind here), so it parses the literal directly.
    return [float(x) for x in raw.strip("[]").split(",")]


def _sample_indices(n: int, sample_size: int) -> list[int]:
    """`sample_size` indices into a sequence of length `n`, evenly spread from 0 to n-1 inclusive
    (both endpoints always included) -- deduplicated, so this can return fewer than `sample_size`
    only if `n` itself is smaller. Used against a length-sorted row list so the resulting sample
    spans the corpus's full short-to-long distribution, never just its two extremes or just its
    middle.
    """
    if n <= sample_size:
        return list(range(n))
    step = (n - 1) / (sample_size - 1)
    return sorted({round(i * step) for i in range(sample_size)})


@pytest.fixture(scope="module")
def real_corpus_sample() -> list[tuple[int, str, list[float]]]:
    """(id, content, stored_embedding) for `_SAMPLE_SIZE` real rows in `documents`, spread across
    the corpus's real length distribution. Module-scoped: this is a read of the unchanging live
    corpus, not something that needs to vary per test.
    """
    conn = psycopg.connect(_database_url())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, length(content) AS len FROM documents ORDER BY len ASC")
            ordered = cur.fetchall()
            if len(ordered) < _SAMPLE_SIZE:
                pytest.skip(
                    f"documents has only {len(ordered)} rows; this test needs the real, "
                    f"fully-ingested corpus (>={_SAMPLE_SIZE} rows) to sample short and long chunks"
                )
            indices = _sample_indices(len(ordered), _SAMPLE_SIZE)
            sample_ids = [ordered[i][0] for i in indices]
            cur.execute(
                "SELECT id, content, embedding::text FROM documents WHERE id = ANY(%s)",
                (sample_ids,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [(row_id, content, _parse_stored_vector(vec)) for row_id, content, vec in rows]


@pytest.fixture(scope="module")
def gguf_embedder() -> GGUFEmbedder:
    settings = get_settings()
    n_ctx = int(os.environ.get("EMBED_GGUF_N_CTX", settings.EMBED_GGUF_N_CTX))
    n_threads = int(os.environ.get("EMBED_GGUF_THREADS", settings.EMBED_GGUF_THREADS))
    return GGUFEmbedder(model_path=_gguf_path(), n_ctx=n_ctx, n_threads=n_threads)


@requires_gguf_model
async def test_gguf_embedder_matches_real_stored_vectors_short_and_long(
    gguf_embedder, real_corpus_sample
):
    """Every sampled chunk -- spanning the corpus's shortest to its longest (up to ~1946 tokens) --
    must match its already-stored vector at cosine >=0.9999. Never trivial: sampling across the
    WHOLE length distribution is what a short-only sample would miss, since the n_batch divergence
    this guards against (GGUFEmbedder's own docstring, point 2) only appears past roughly 512
    tokens.
    """
    assert len(real_corpus_sample) >= 20, (
        f"expected a real sample of roughly {_SAMPLE_SIZE} chunks, got only "
        f"{len(real_corpus_sample)} -- check the corpus is actually fully ingested"
    )
    below_threshold = []
    for row_id, content, stored in real_corpus_sample:
        [got] = await gguf_embedder.embed([content])
        cosine = _cosine(got, stored)
        if cosine < _MIN_COSINE:
            below_threshold.append((row_id, len(content), round(cosine, 6)))
    assert not below_threshold, (
        f"{len(below_threshold)} of {len(real_corpus_sample)} sampled chunks fell below cosine "
        f"{_MIN_COSINE} against their real stored vector (id, content length, cosine): "
        f"{below_threshold}"
    )


@requires_gguf_model
async def test_gguf_embedder_raises_rather_than_truncates_an_overlong_input():
    """A GGUFEmbedder given an input that tokenizes to more tokens than its configured n_ctx must
    RAISE, naming both the real token count and the configured limit -- never silently return a
    vector for a truncated prefix of the text (a silently-truncated vector is wrong in a way
    nothing downstream can detect, see GGUFEmbedder's own docstring). Uses the real GGUF file (a
    fake one cannot be tokenized) but a tiny n_ctx so this needs no long fixture text and stays
    fast.
    """
    embedder = GGUFEmbedder(model_path=_gguf_path(), n_ctx=16, n_threads=1)
    overlong_text = " ".join(f"word{i}" for i in range(200))  # tokenizes to far more than 16
    with pytest.raises(ValueError) as exc_info:
        await embedder.embed([overlong_text])
    message = str(exc_info.value)
    assert (
        "16" in message
    ), f"expected the configured 16-token limit named in the error: {message!r}"
    # The real token count must be named too -- assert only that SOME number greater than the
    # 16-token limit appears, never a hardcoded expected count (that would tie this test to one
    # exact tokenizer output rather than to the real "raise, don't truncate" behavior under test).
    token_counts = [int(n) for n in re.findall(r"(\d+) tokens", message)]
    assert (
        token_counts and token_counts[0] > 16
    ), f"expected a real token count over the 16-token limit named in the error: {message!r}"


@requires_gguf_model
async def test_gguf_embedder_raises_before_computing_anything_for_a_batch_with_one_overlong_input():
    """The over-budget check runs on EVERY text in a batch before any embedding is computed for
    the batch at all -- a short, valid text sharing a call with one overlong text must not silently
    return a partial/wrong result for the short text either; the whole call raises.
    """
    embedder = GGUFEmbedder(model_path=_gguf_path(), n_ctx=16, n_threads=1)
    short_text = "a short valid question"
    overlong_text = " ".join(f"word{i}" for i in range(200))
    with pytest.raises(ValueError):
        await embedder.embed([short_text, overlong_text])
