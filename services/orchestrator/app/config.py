"""Environment-driven settings.

Every value the orchestrator needs at runtime is read from here, and nowhere else. In particular,
EMBED_MODEL is read from this single place by both ingest.py and pipeline.py: the corpus and the
query must always land in the same embedding space, so the model name is single-sourced rather than
configured twice (see ARCHITECTURE.md, "The corpus and the query always use the same embedding
model").
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    DATABASE_URL: str = "postgresql://officehours:officehours@localhost:5432/officehours"

    # Providers
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434"
    EMBED_PROVIDER: str = "ollama"
    EMBED_MODEL: str = "nomic-embed-text"
    EMBED_DIM: int = 768
    LLM_PROVIDER: str = "ollama"
    LLM_MODEL: str = "qwen3.5-8k:latest"

    # Provider timeouts (read/request timeout; a short connect timeout is fixed in the provider
    # code separately). The local Ollama generator can spend several minutes deliberating on an
    # advice-seeking question before responding, so this defaults high rather than to httpx's 5s
    # default. Configurable per-environment: a hosted provider in production should not need
    # anywhere near this long.
    LLM_TIMEOUT_SECONDS: float = 600.0
    EMBED_TIMEOUT_SECONDS: float = 180.0

    # How long Ollama keeps the generator model loaded in memory after a request, in Ollama's own
    # duration syntax (e.g. "60m", "10m", or "-1" to keep it loaded forever). Sent on every
    # /api/chat request so a fresh clone gets the same behavior as a host with OLLAMA_KEEP_ALIVE
    # set in its own environment: without it, Ollama's default (a few minutes) unloads the model
    # between the eval harness's sequential rows, and every row pays a cold-load cost on top of
    # generation time.
    OLLAMA_KEEP_ALIVE: str = "60m"

    # Whether the generator is allowed to emit a "thinking" block before its answer. The
    # generator (a reasoning model) will, on a meaningful fraction of questions, spend its whole
    # output budget deliberating inside that block and get cut off before writing any content --
    # Ollama still returns HTTP 200, but with an empty content string. A grounded answer assembled
    # from retrieved context does not need chain-of-thought, so this defaults to False. Sent on
    # every /api/chat request, the same way OLLAMA_KEEP_ALIVE is.
    OLLAMA_THINK: bool = False

    # Retrieval
    RETRIEVAL_TOP_K: int = 5

    # Hybrid RRF (Phase 3). RRF_K is the constant k in 1 / (k + rank) from the original
    # Cormack/Clarke/Buettcher RRF paper; 60 is that paper's own constant, not tuned against this
    # corpus or the golden set (see docs/adr/0001-rrf-vs-weighted-blend.md).
    RRF_K: int = 60

    # HYBRID_CANDIDATE_POOL is how deep each arm (semantic, keyword) looks before ranks are fused,
    # 4x RETRIEVAL_TOP_K. It changes how many candidates each arm contributes to the fusion, NOT
    # how many chunks the generator sees -- that stays RETRIEVAL_TOP_K, which must not change in
    # Phase 3: the Phase 1 semantic-only baseline (eval/results/20260830T183110Z.json) is compared
    # against without being re-run, and changing top_k would invalidate that comparison.
    HYBRID_CANDIDATE_POOL: int = 20

    # Eval judge (Phase 1). Hosted NVIDIA endpoint, a different model family than the generator,
    # so a self-preference bias never creeps into the eval gate (see ARCHITECTURE.md, "The eval
    # judge is a hosted model from a different family than the generator"). JUDGE_API_KEY has no
    # default: an absent key must arrive empty and fail loudly at run time in eval/judge.py, never
    # silently fall back to the local generator.
    JUDGE_PROVIDER: str = "nvidia"
    JUDGE_BASE_URL: str = "https://integrate.api.nvidia.com/v1"
    JUDGE_API_KEY: str = ""
    JUDGE_MODEL: str = "nvidia/nemotron-3.5-lightning-30b-a3b"

    # The NVIDIA judge endpoint's observed limit is roughly 40 requests/minute. This is the single
    # rate the whole eval run's judge traffic is capped at -- both eval/judge.py's own two judged
    # tasks and every RAGAS-internal judge call share one limiter built from this value (see
    # eval/judge.py::SHARED_RATE_LIMITER) -- so it has to stay comfortably under 40, not track it
    # closely. 20 is half the observed limit; raise it only with real headroom measured, never back
    # up against the observed ceiling.
    JUDGE_REQUESTS_PER_MINUTE: int = 20

    # How many attempts eval/judge.py's own judge calls (score_comprehensibility,
    # classify_refusal, judge_selfcheck) make before giving up on a 429/5xx/timeout and recording
    # the row as errored. A row losing its judge call after this many attempts is still recorded as
    # a genuinely errored row (see eval/run.py) -- raising this only buys more retries against a
    # transient rate limit, it never papers over a real failure.
    JUDGE_MAX_RETRIES: int = 12

    # Observability
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4318"
    OTEL_SERVICE_NAME: str = "office-hours-orchestrator"

    # Ingestion / crawling
    CRAWL_DELAY_SECONDS: float = 2.0
    USER_AGENT: str = "OfficeHoursBot/0.1 (+https://github.com/office-hours)"

    # Filesystem
    SOURCES_MANIFEST_PATH: str = "/app/data/sources/sources.yaml"
    RAW_SNAPSHOT_DIR: str = "/app/data/sources/raw"

    # Ingestion mode. "fetch" (default) reads SOURCES_MANIFEST_PATH and fetches each URL over
    # HTTP, as Phase 0 always has. "snapshot" skips the manifest and the network entirely: it
    # treats every *.md file already in RAW_SNAPSHOT_DIR as a snapshot (frontmatter + body, the
    # exact format _fetch_and_snapshot writes) and chunks/embeds each one directly. This is what
    # lets the CI eval gate ingest eval/fixtures/sources/ -- a small, committed corpus -- without
    # fetching any live URL (see docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md).
    INGEST_MODE: str = "fetch"


@lru_cache
def get_settings() -> Settings:
    return Settings()
