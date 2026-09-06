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

    # No-answer guardrail (Phase 4). If retrieval returns nothing, or the MINIMUM cosine distance
    # across every retrieved chunk exceeds this, app/pipeline.py returns NO_ANSWER and never calls
    # the generator at all -- ARCHITECTURE.md's "the system says when it does not know". Gated on
    # the minimum across all retrieved chunks, not the RRF-top-1 chunk's own distance: RRF-top-1 is
    # a fused-rank quantity, not a semantic-closeness one, and can be noisier than the single
    # closest chunk actually retrieved.
    #
    # 0.50, derived from the 7 off-topic control queries ALONE (sourdough, capital of France, car
    # insurance, vitamin D, World Cup, faucet, game programming), measured against the live corpus
    # and real nomic-embed-text embeddings: 0.4370, 0.5251, 0.5315, 0.5358, 0.5431, 0.5540, 0.5668.
    # Six of the seven sit in a tight cluster from 0.5251 up; 0.50 sits just below that cluster.
    # This is an honest, accepted gap, not an oversight: 0.50 misses the "car insurance" control at
    # 0.4370, and CLAUDE.md's carve-out means the fix for that miss is prompt rule 3 ("say plainly
    # when the sources do not cover it", app/prompts.py::SYSTEM_PROMPT), which stays the PRIMARY
    # "sources do not cover it" mechanism -- not a lower threshold, because a threshold low enough
    # to also catch "car insurance" starts suppressing real, answerable questions (see next
    # paragraph). The 7 in-domain control queries this project also measured (0.1563-0.3814) are
    # nowhere near this threshold either way and were not used to pick it.
    #
    # The golden set (eval/golden.jsonl) was measured only AFTER 0.50 was chosen from the controls
    # above, to check the threshold's consequence, never to pick the threshold itself. That check
    # is exactly why an earlier value of 0.42 was rejected: at 0.42, three real, answerable golden
    # rows have a best-retrieved-chunk distance above threshold and would have been wrongly
    # suppressed -- row 16 ("How does the wage-weighted lottery work?", 0.4720), row 0 ("What is
    # the I-983 and who fills it out?", 0.4348), and row 7 ("Should my employer put me in at a
    # higher wage level...", 0.4344); every other golden row sits below 0.36, and the best sits at
    # 0.1748. At 0.47 only row 16 still fires; at 0.50 none do. Row 0 matters most: its answer lives
    # in a single chunk (out of 216) that contains the token "-983", so its semantic signal is
    # genuinely weak -- that chunk is exactly what Phase 3's keyword arm exists to surface (see
    # docs/adr/0001-rrf-vs-weighted-blend.md), and gating retrieval on semantic distance alone would
    # silently undo that fix. A second gate on the keyword arm's ts_rank_cd was checked and rejected
    # too: row 0 scores ts_rank_cd 3.6 over 3 keyword hits, the "car insurance" off-topic control
    # scores a similar 3.4 over 6 hits, and "game programming" scores a HIGHER 7.2 -- no threshold
    # on either arm, or on both together, separates row 0 from the off-topic controls, so this is
    # not a gap a cleverer mechanical check can close; it is why prompt rule 3 has to carry it.
    #
    # CI's stub embedder (EMBED_PROVIDER=stub) is a content-blind hash with no semantic meaning at
    # all -- measured top-1 distances of 0.92-0.95 against the 17-chunk fixture corpus, which would
    # fire this path on every single CI row under 0.50 and destroy the recorded CI baseline. See
    # .github/workflows/eval.yml's ci-invariant-gate job, which overrides this to "2.0" (the maximum
    # possible cosine distance, so the gate can never fire there) for exactly that reason; the
    # no-answer path itself is exercised by dedicated tests instead of the CI golden run.
    NO_ANSWER_MAX_DISTANCE: float = 0.50

    # Clarifier (Phase 4, app/guardrails/clarifier.py). A query needs at least this many content
    # words, after generic English stopwords are stripped, before it is considered specific enough
    # to retrieve against; fewer than this returns CLARIFY (one clarifying question, no retrieval)
    # unless the query also names a recognized topic anchor (a visa/status code, a form number, or
    # a topic noun this corpus covers) alongside at least one other content word. Deliberately
    # small: a false-positive clarify on a real, short factual question costs the person an extra
    # round trip for no reason, which is worse than occasionally letting a genuinely vague query
    # through to retrieval instead.
    CLARIFY_MIN_CONTENT_WORDS: int = 3

    # Scheduled refresh job (Phase 5, app/recrawl.py, [freshness] extra only -- never installed by
    # the service image or by CI's invariant gate). REFRESH_CHECKPOINT_PATH is where LangGraph's
    # AsyncSqliteSaver persists per-source state, so a killed run resumes instead of re-fetching
    # every source from scratch. REFRESH_MAX_FETCH_ATTEMPTS bounds the fetch node's own retry
    # loop -- never an unbounded retry. REFRESH_RETRY_BACKOFF_SECONDS is the sleep between attempts;
    # tests set this to 0 so retries are instant and deterministic.
    REFRESH_CHECKPOINT_PATH: str = "/app/data/refresh-checkpoints.sqlite"
    REFRESH_MAX_FETCH_ATTEMPTS: int = 3
    REFRESH_RETRY_BACKOFF_SECONDS: float = 2.0

    # Broken vs. merely overdue (Phase 7, app/guardrails/freshness.py::source_health_state). A
    # source is BROKEN when consecutive_failures >= this, or status == 'robots_disallowed', or
    # last_success_at is older than SOURCE_BROKEN_NO_SUCCESS_DAYS. Both numbers are derived from
    # facts already fixed elsewhere, not invented for this check:
    #
    # - 3 consecutive failures: the refresh cron is daily (.github/workflows/recrawl.yml,
    #   `17 8 * * *`), so three consecutive failures means three days running -- past where a
    #   single transient network blip or one bad night explains it.
    # - 7 days with no success: not a new number. It is exactly the existing "recent" -> "stale"
    #   boundary sources_freshness_state already uses (168 hours), so the two signals agree with
    #   each other instead of the codebase carrying two different staleness timescales that could
    #   drift apart.
    # - robots_disallowed is broken immediately, with no failure-count threshold at all: it is
    #   permanent by nature (nothing about retrying makes a disallowed URL fetchable again), so
    #   there is no count of attempts that would make it any less broken than the first one.
    SOURCE_BROKEN_CONSECUTIVE_FAILURES: int = 3
    SOURCE_BROKEN_NO_SUCCESS_DAYS: int = 7

    # CORS (Phase 6). The Next.js frontend (services/frontend) calls the orchestrator directly from
    # the browser -- the same path the Phase 7 Go gateway will later sit in front of -- so the
    # orchestrator has to answer the browser's preflight itself until then. Comma-separated list of
    # allowed origins; the default covers the frontend's local dev server only.
    ALLOWED_ORIGINS: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
