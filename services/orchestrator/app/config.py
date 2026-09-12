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

    # --- Phase 8 round 8: in-process GGUF embeddings (EMBED_PROVIDER=gguf), production's actual
    # embedding provider (docs/adr/0013-in-process-gguf-embeddings.md). Neither Ollama Cloud nor
    # NVIDIA serves nomic-embed-text, and the 221 stored vectors in `documents` are nomic-embed-text
    # 768-dim, so production loads the IDENTICAL GGUF file Ollama itself uses (baked into the
    # image, see services/orchestrator/Dockerfile) and embeds with it in-process via
    # llama-cpp-python -- no API, no vendor, no third machine. Empty EMBED_GGUF_PATH is fine for a
    # fresh local clone: EMBED_PROVIDER defaults to "ollama" below, so
    # app/providers/embeddings.py::GGUFEmbedder is never constructed unless EMBED_PROVIDER=gguf is
    # set explicitly.
    #
    # EMBED_GGUF_N_CTX is the ONE setting n_batch and n_ubatch are both derived from
    # (GGUFEmbedder always constructs llama_cpp.Llama with n_batch=n_ubatch=n_ctx=this value) --
    # see GGUFEmbedder's own docstring for why: llama.cpp's own default n_batch=512 silently splits
    # any longer sequence into multiple batches and returns a DIFFERENT vector for it, measured
    # directly against 25 real stored corpus vectors (5 of 25 fell below cosine 0.9999, min 0.974,
    # every one >=583 tokens; every chunk <=485 tokens matched). Exposing n_batch/n_ubatch as
    # separate settings would let them silently drift apart from n_ctx in some future .env edit;
    # deriving all three from one number makes that impossible. 2048 is the measured serving
    # config (351MB loaded / 358MB peak, 16.2ms median query embed at 1 thread) and comfortably
    # covers this corpus's longest chunk (1946 tokens); a future re-ingest with EMBED_PROVIDER=gguf
    # embedding long chunks would need this raised (the measured ingest config, 8192, peaks at
    # 742MB) -- never lowered to save memory without checking it still covers every stored chunk's
    # token count. GGUFEmbedder raises rather than silently truncating an overlong input either way.
    EMBED_GGUF_PATH: str = ""
    EMBED_GGUF_N_CTX: int = 2048
    # 1 is the exact thread count the 351MB/16.2ms measurement above was taken at; Fly's
    # shared-cpu-1x (this project's production VM size) has one shared vCPU, so more threads add
    # scheduling overhead rather than real parallelism.
    EMBED_GGUF_THREADS: int = 1

    # Ollama Cloud auth (Phase 8 production primary). Empty by default: local Ollama needs no
    # auth, so app/providers/llm.py::OllamaLLM sends no Authorization header at all unless this is
    # set. Production points OLLAMA_BASE_URL at https://ollama.com and sets this to a real Ollama
    # Cloud API key, sent as `Authorization: Bearer $OLLAMA_API_KEY` on every /api/chat call.
    OLLAMA_API_KEY: str = ""

    # Whether OLLAMA_BASE_URL is Ollama Cloud rather than a local Ollama install. False by default,
    # which is what keeps a fresh local clone byte-for-byte unchanged: keep_alive and think are
    # local-Ollama-only concerns (see those two settings' own comments below). This project HAS
    # confirmed Ollama Cloud's real /api/chat behavior for both (see docs/reports/phase-8.md): a
    # direct probe against the live cloud backend with `{"model": "gpt-oss:120b-cloud", "think":
    # false, "keep_alive": "60m", "stream": false}` returned HTTP 200 -- both fields are ACCEPTED,
    # neither causes an error -- but `"think": false` does NOT suppress gpt-oss's reasoning block:
    # the response carried a populated `thinking` field (243 characters) alongside a non-empty
    # `content` field (190 characters) regardless. So the empty-answer failure mode `OLLAMA_THINK
    # =false` exists to prevent locally (a reasoning model spending its whole output budget inside
    # the thinking block and returning empty content, see OLLAMA_THINK's own comment) does not occur
    # with gpt-oss on Ollama Cloud either way. Setting this True still makes
    # app/providers/llm.py::OllamaLLM omit both fields from the request: keep_alive is meaningless
    # against a hosted endpoint (Ollama Cloud manages its own model lifecycle, not this process's),
    # and think has nothing to gain by being sent -- it neither suppresses the reasoning block nor
    # prevents any failure mode there -- so omitting both stays the simplest correct choice, now
    # confirmed rather than assumed.
    OLLAMA_CLOUD: bool = False

    # Fallback generator (Phase 8 production only). Empty LLM_FALLBACK_PROVIDER (the default) means
    # no fallback at all: get_llm() returns the bare primary provider, exactly as it always has, so
    # a fresh local clone's behavior is untouched. When set, app/providers/llm.py wraps the primary
    # and this fallback in a FallbackLLM that tries the primary first and this second, exactly once
    # each. Production sets LLM_FALLBACK_PROVIDER="nvidia" (any value other than "ollama"/"hosted"/
    # "stub" is treated as a generic OpenAI-compatible /v1/chat/completions endpoint -- see
    # app/providers/llm.py::_build_provider), LLM_FALLBACK_MODEL="openai/gpt-oss-20b",
    # LLM_FALLBACK_BASE_URL="https://integrate.api.nvidia.com/v1/chat/completions" (the FULL
    # completions URL, not a base to append a path onto), and LLM_FALLBACK_API_KEY to a real NVIDIA
    # API key.
    LLM_FALLBACK_PROVIDER: str = ""
    LLM_FALLBACK_MODEL: str = ""
    LLM_FALLBACK_BASE_URL: str = ""
    LLM_FALLBACK_API_KEY: str = ""

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

    # Dated-rule companion retrieval (docs/adr/0019-dated-rule-companion-retrieval.md). The DHS
    # fixed-period-of-admission final rule (effective 2026-09-15, changing the F-1
    # post-completion departure period from 60 days to 30) sits in three chunks that carry a
    # `rule_effective_date` and that measurably lose the RRF fusion race on plain departure-period
    # questions -- "What is the grace period after OPT ends?" retrieves only the still-current
    # 60-day chunks in the fused top-k, never the dated replacement, which violates the
    # "answers state both the current rule and its dated replacement" rule (ARCHITECTURE.md).
    # app/db.py::hybrid_search's `companions` CTE fixes this directly: once the fused top-k
    # contains ANY chunk carrying a rule_effective_date, it adds up to this many MORE chunks that
    # carry that SAME date, ordered by raw cosine distance, regardless of whether they fused well.
    #
    # 2, not 1: measured directly against "What is the grace period after OPT ends?" -- the single
    # closest dated chunk by distance is id 670, which discusses the rule but does not itself
    # state the new 30-day number; the chunk that actually states it sits one further out, behind
    # 670. A companion count of 1 admits 670 alone and still fails to surface the replacement
    # figure; 2 admits both. n=2 was fitted to a fixed acceptance ladder (see the ADR for exactly
    # which queries and why that is a legitimate way to pick it here) -- treat it as a floor to
    # revisit, not a constant with headroom to spare, the next time a new dated rule lands in the
    # corpus.
    #
    # 0 disables the feature entirely: app/db.py's `companions` CTE returns zero rows (a `LIMIT 0`
    # regardless of its WHERE clause), and hybrid_search's output is byte-identical to the
    # pre-companion behavior. app/pipeline.py's no-answer gate ignores companion chunks
    # unconditionally, by construction, regardless of this setting's value -- see hybrid_search's
    # own docstring and RetrievedChunk.retrieved_by.
    DATED_RULE_COMPANIONS: int = 2

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

    # Logging (Phase 8). Under uvicorn, `logging.basicConfig` is never called by anything unless
    # app/main.py does it itself: uvicorn's own default logging config
    # (uvicorn.config.LOGGING_CONFIG) configures the "uvicorn"/"uvicorn.error"/"uvicorn.access"
    # loggers only, and leaves the root logger untouched (level WARNING, no handler). Every
    # `logger.info(...)` in app/* (in particular app/providers/llm.py::FallbackLLM's
    # `llm.primary_failed`/`llm.served_by` lines, which name which provider actually served a
    # request) propagates to that unconfigured root logger and is silently discarded -- confirmed
    # against a running container (effective level for app.providers.llm was WARNING, root had zero
    # handlers). app/main.py calls `logging.basicConfig(level=LOG_LEVEL, ...)` at import time,
    # before the FastAPI app object is built, so the root logger has both a level and a handler
    # before the first request can possibly arrive. Defaults to INFO so a fresh clone's
    # `docker logs` shows the provider-served-by line without any extra configuration.
    LOG_LEVEL: str = "INFO"

    # Ingestion / crawling
    CRAWL_DELAY_SECONDS: float = 2.0
    USER_AGENT: str = "OfficeHoursBot/0.1 (+https://github.com/office-hours)"

    # Filesystem
    SOURCES_MANIFEST_PATH: str = "/app/data/sources/sources.yaml"
    RAW_SNAPSHOT_DIR: str = "/app/data/sources/raw"

    # Golden-set impact reporting (Phase 8 round 2, app/recrawl.py). Read-only: app/recrawl.py
    # never writes to this path, it only reads each row's `source_urls` field to report which
    # golden rows a meaningfully-changed source might affect. Container-absolute by default, the
    # same convention as SOURCES_MANIFEST_PATH/RAW_SNAPSHOT_DIR above -- docker-compose.yml already
    # bind-mounts ./eval to /app/eval, so this default resolves correctly there with no extra
    # wiring; .github/workflows/recrawl.yml overrides it to a checkout-relative path the same way
    # it already overrides RAW_SNAPSHOT_DIR/SOURCES_MANIFEST_PATH.
    GOLDEN_SET_PATH: str = "/app/eval/golden.jsonl"

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

    # --- Phase 8 round 3: semantic cache (app/cache.py) ---
    # Off by default: a fresh clone's `docker compose up` and the CI invariant gate are unaffected
    # either way, since app/pipeline.py never calls app/cache.py at all unless this is True.
    SEMANTIC_CACHE_ENABLED: bool = False

    # Cosine-distance threshold below which a cached entry is served instead of re-generating.
    # Derived from real measurements against the real nomic-embed-text embedder (the same one the
    # query path uses), never picked -- see docs/reports/phase-8.md for the full measurement
    # output. Two pools were measured, both over eval/golden.jsonl's 21 real questions:
    #
    # 1. "Same-intent" distances: 12 of the 21 golden questions, each paired with a natural
    #    paraphrase written for this measurement (different wording, same underlying question --
    #    e.g. "How long is the STEM OPT extension?" vs. "How many months does the STEM OPT
    #    extension last?"). min=0.0394, max=0.1944, mean=0.1041 across the 12 pairs.
    # 2. "Different-question" distances: every one of the 210 distinct pairs among those same 21
    #    golden questions. The CLOSEST pair -- the real near-miss this threshold has to stay under
    #    -- is 0.1595, between "Should I leave my current job for one at an E-Verify employer so I
    #    can get the STEM extension?" (advice-seeking) and "Does my employer need E-Verify for the
    #    STEM extension?" (purely informational). That is exactly the pair a false cache hit would
    #    be most dangerous for: it would serve one question's response_type (REFUSAL_ADVICE or
    #    ANSWER) for the other, crossing the advice/information line this project treats as a hard
    #    safety boundary, not a quality nuance.
    #
    # These two pools OVERLAP (0.1944 > 0.1595): there is no single threshold that catches every
    # measured paraphrase while missing every measured different-question pair. 0.15 is chosen to
    # sit strictly below the measured 0.1595 near-miss, with a small margin, rather than at the
    # edge of it -- the honest cost is that 2 of the 12 measured paraphrases (0.1944 and 0.1599)
    # now sit ABOVE this threshold and would MISS the cache (a real repeat question, re-generated
    # instead of served from cache). That is an accepted, asymmetric cost: a false miss only costs
    # one extra generation call; a false hit on this specific near-miss pair would answer an
    # advice-seeking question with a cached factual response or vice versa, which this project
    # cannot tolerate at any rate greater than the guardrail pipeline's own. 10 of the 12 measured
    # paraphrases (min 0.0394) still hit at this threshold.
    SEMANTIC_CACHE_SIMILARITY_THRESHOLD: float = 0.15

    # --- Phase 8 round 3: cheap-model routing for the Layer 2 advice classifier ---
    # Empty by default (both), which is what keeps a fresh clone byte-for-byte unchanged:
    # app/providers/llm.py::get_classifier_llm returns None when CLASSIFIER_LLM_PROVIDER is empty,
    # and app/pipeline.py falls back to the SAME generator LLM_MODEL for Layer 2 exactly as it
    # always has (see app/guardrails/classifier.py -- classify_advice itself is unchanged; only
    # WHICH LLM object it is handed differs). Configured, not hardcoded: production sets
    # CLASSIFIER_LLM_PROVIDER=ollama, CLASSIFIER_LLM_MODEL to a cloud-tagged small model (Ollama
    # Cloud lists gpt-oss:20b and gemma4 as cloud-tagged options; either is a reasonable choice for
    # a binary classification task).
    #
    # DELIBERATE, DOCUMENTED DECISION: the classifier model is NOT included in
    # app/providers/llm.py::resolve_generator_models, and therefore never checked by
    # assert_no_generator_judge_family_collision (the generator/judge family guard). This is
    # intentional, not an oversight: eval/judge.py's judge calls (score_comprehensibility,
    # classify_refusal, and every RAGAS metric) score ONLY the generator's own answer text
    # (`answer_text` in app/pipeline.py) -- the classifier's raw output (a `{"advice": true|false}`
    # JSON blob) is parsed into a boolean, recorded on the classify span's `advice_decided_by`
    # attribute, and then discarded; it never becomes part of any text the judge is asked to score.
    # A judge favoring its own family's writing style (the self-preference bias the guard exists to
    # prevent) has no surface to act on here, because the judge never sees anything the classifier
    # wrote. See services/orchestrator/tests/test_model_family_guard.py for the test asserting this
    # exclusion holds (a CLASSIFIER_LLM_MODEL that WOULD collide with the judge's family must NOT
    # raise), so a future edit that widens the guard to include it would have to consciously break
    # that test rather than silently regress this reasoning.
    CLASSIFIER_LLM_PROVIDER: str = ""
    CLASSIFIER_LLM_MODEL: str = ""

    # --- Phase 8 round 3: daily generation budget cap (app/usage.py, read by app/pipeline.py) ---
    # 0 means "no cap" -- the default, which is what keeps a fresh clone and the CI invariant gate
    # unaffected: app/usage.py::budget_exceeded returns False immediately (no database round trip)
    # whenever this is <= 0. When set to a positive integer, app/pipeline.py refuses to call the
    # generator once today's UTC count of successful generation calls reaches this value, and
    # degrades instead (serves from the semantic cache if there is a hit, otherwise returns a
    # response naming the retrieved sources without generating a summary of them -- see
    # app/pipeline.py's module docstring for the exact shape of that response).
    DAILY_GENERATION_CAP: int = 0

    # --- Phase 8 round 3: anonymous session hashing (app/usage.py) ---
    # HMAC-SHA256 key for app/usage.py::hash_session_identifier. This default is fine for local dev
    # (there is nothing sensitive to protect on a machine only its own developer can reach) but
    # MUST be overridden with a real, random secret in production, the same way every other secret
    # in this project is: an environment variable, never a hardcoded production value, never
    # committed. See hash_session_identifier's own docstring for exactly what having this secret
    # does and does not protect against.
    SESSION_HASH_SALT: str = "office-hours-dev-salt-change-in-production"

    # --- Phase 8 round 4: production observability, Grafana Cloud (OTLP) + Langfuse ---
    # OTEL_EXPORTER_OTLP_ENDPOINT/OTEL_EXPORTER_OTLP_ENDPOINT already exist above; no new setting
    # needed for the Grafana Cloud switch itself (see app/telemetry.py's own comment, "THE URL
    # RULE", for the one thing that changed there). OTEL_EXPORTER_OTLP_HEADERS/
    # OTEL_EXPORTER_OTLP_PROTOCOL are read directly by the OTel SDK from the process environment
    # (confirmed: OTLPSpanExporter/OTLPMetricExporter apply OTEL_EXPORTER_OTLP_HEADERS even when
    # `endpoint` is passed explicitly), so neither needs a Settings field of its own either.
    #
    # Langfuse (app/langfuse_telemetry.py): all three empty by default, which is what keeps a fresh
    # clone's `docker compose up` and the CI invariant gate unaffected -- app/langfuse_telemetry.py
    # ::setup_langfuse only ever builds a real client when BOTH LANGFUSE_PUBLIC_KEY and
    # LANGFUSE_SECRET_KEY are non-empty.
    LANGFUSE_HOST: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
