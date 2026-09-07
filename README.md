# Office Hours

Office Hours answers factual questions about F-1, OPT, STEM OPT, and H-1B rules for international
students and workers, using only official U.S. government sources, and it cites the specific source
behind every claim. It tells you what a rule says and where it's written. It does not tell you what
to do, whether your case will be approved, or which status fits your situation: questions like that
get a refusal that points to your school's designated school official (DSO) or a licensed
immigration attorney.

This is an unofficial tool. It is not affiliated with USCIS, DHS, or any other part of the U.S.
government, and nothing it says is legal advice. Every answer carries that disclaimer, and no query
is stored with any identifying information.

## Architecture

The request path is one line from browser to database. The LLM and observability providers hang off
the two backend services as side calls, not additional hops in the main path.

```
Browser
   |
   v
Vercel  (Next.js frontend)
   |
   v
Fly: gateway  (Go, chi router)
   - Redis token bucket, empty bucket -> 429 immediately
   - PII redaction on the request body
   - context.WithTimeout(15s) on the upstream call, never time.Sleep for a retry
   - W3C trace propagation (traceparent)
   |
   v
Fly: orchestrator  (FastAPI)
   clarify -> classify -> retrieve -> no-answer gate -> generate -> verify citations -> freshness
   |
   v
Neon Postgres + pgvector
   HNSW (semantic) and tsvector (keyword), fused with Reciprocal Rank Fusion in one CTE

Side calls:
   gateway      -> Upstash Redis          (token bucket state)
   gateway      -> Grafana Cloud (OTLP)   (trace export)
   orchestrator -> Ollama Cloud           (primary generation)
   orchestrator -> NVIDIA                 (fallback generation, and the eval judge)
   orchestrator -> Grafana Cloud (OTLP)   (trace export)
   orchestrator -> Langfuse               (LLM-level traces)
```

## The eval numbers

These are from one real eval run against the hosted production provider, compared against the last
run recorded before that provider swap. Both runs score all 21 rows of `eval/golden.jsonl`, judged by
`nvidia/nemotron-3.5-lightning-30b-a3b`, a different model family than either generator, so the judge
is never grading its own family's writing.

- Hosted: `eval/results/20260907T015332Z.json`, generating on Ollama Cloud `gpt-oss:120b`, against the
  live 221-chunk, 14-source corpus in the local Postgres container (not yet on Neon, see below).
- Phase 4 baseline: `eval/results/20260905T234405Z.json`, generating locally on `qwen3.5-8k`.

Both runs embedded the query with real local Ollama (`EMBED_PROVIDER=ollama`), not yet the in-process
GGUF embedder production now uses (`EMBED_PROVIDER=gguf`, decided below). That is deliberate, not an
oversight: `GGUFEmbedder` is built to reproduce Ollama's own vectors, not a new embedding space, and
the equivalence test (`services/orchestrator/tests/test_gguf_embedder.py`) checks that directly against
the real stored corpus rather than by re-running the eval. What that check measured: against 25 real
stored vectors the in-process embedder reproduces them at a minimum cosine of 0.99999412, and across
five real queries both embedders retrieve the identical chunk set in the identical order, with the
off-topic control returning `no_answer` under both. Retrieval is the same, so these numbers describe
a GGUF-embedded run as well.

| metric | hosted | P4 baseline |
| --- | --- | --- |
| faithfulness | 0.875 | 0.841 |
| answer_relevancy | 0.641 | 0.600 |
| context_precision | 0.905 | 0.937 |
| false_refusal_rate | 0.067 | 0.400 |
| advice_leakage_rate | 0.000 | 0.167 |
| comprehensibility | 3.333 | 3.190 |
| citation_hallucination_rate | 0.000 | 0.000 |
| unreferenced_citation_rate | 0.686 | 0.486 |
| reading_grade_level | 15.128 | 17.603 |
| median /query latency | 1.93s | 7.07s |

Most of these moved the direction you'd want. False refusal dropped the most, from 40% of factual
questions wrongly refused down to 6.7%; the same guardrail rule layer ran in both runs, so the gap is
closer to how the generator writes near the advice/information boundary than a change in that rule
layer itself. The 7x latency drop is the hosted API against a local reasoning model, not anything this
codebase optimized.

Two numbers got worse, and they're reported as measured, not softened. Context precision, how much of
what got retrieved was actually relevant per RAGAS, fell from 0.937 to 0.905; both clear the 0.70 gate,
but the direction is wrong. Unreferenced citation rate, how many of the 5 chunks the pipeline always
returns as citation candidates go uncited in the answer text, rose from 0.486 to 0.686:
`citation_detail` in the two result files shows the hosted run cited 33 of 105 possible citations
against 54 of 105 for the baseline, so `gpt-oss` references fewer of the retrieved passages per
question, not more.

`citation_hallucination_rate` reads 0.000 on both runs. That is not a model achievement.
`services/orchestrator/app/guardrails/citations.py::verify_citations` blocks any answer that cites an index outside what
was actually retrieved, before it ever renders, regardless of which model wrote it. This number
measures whether that guardrail's boundary held, not whether the model behaved.

## Decisions and why

**One Postgres, no dedicated vector database.** Retrieval fuses a pgvector HNSW ranking and a
`tsvector` keyword ranking with Reciprocal Rank Fusion, in a single SQL CTE, over one Postgres
instance holding 14 sources. A separate vector store would add a second consistency problem and a
second thing to deploy, for a corpus this size. `docs/adr/0001-rrf-vs-weighted-blend.md`.

**A hard line between information and advice.** The system states what a rule says and where it's
written. It refuses to say what someone personally should do, whether their case will be approved, or
which status fits them, and redirects those questions to a DSO or an attorney.
`docs/adr/0002-advice-vs-information-line.md`.

**Chunking follows document structure, not heading level numbers.** Sources are chunked on h2 through
h4, and a chunk's parent comes from document order and empty group-label headings, never from
comparing level numbers. One FAQ page nests h2 questions under h3 group labels; a couple of USCIS
pages carry their real section boundaries in h4 with no h2 at all. A level-keyed stack mis-parents
both. `ARCHITECTURE.md`, "Corpus and chunking."

**Two freshness columns, not one.** `fetched_at` is when a page was last downloaded; `last_verified_at`
is when it was last checked, changed or not. A re-crawl that finds nothing new updates only the second
column, so a page checked this morning and found unchanged doesn't read as months stale.
`ARCHITECTURE.md`, "Freshness fields."

**Go at the edge, Python behind it.** The gateway's job is concurrent connection handling under a rate
limit; the orchestrator's job is IO-bound calls to a model and a database. Each language fits its own
half. `docs/adr/0007-go-python-split.md`.

**CI checks a baseline, not the real target.** A pull request job gets no GPU and no judge API key, so
it can't compute faithfulness or comprehensibility at all. It checks retrieval, citation mapping,
refusal bookkeeping, and error handling against the last accepted CI run instead, and its own banner
says plainly that a green result there isn't a passing quality eval. The real target,
`eval/README.md`'s `THRESHOLDS`, is only ever checked by a full run, the kind this README's numbers
came from. `docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md`.

**The judge can't share a family with the generator, and a run refuses to start if it does.**
`assert_no_generator_judge_family_collision` (`services/orchestrator/app/providers/llm.py`) checks every model in the
resolved generation chain, primary and fallback, against the judge's family before a single judge or
generation call happens. A model grading its own family's writing tends to prefer it, for reasons that
have nothing to do with the actual quality of the answer. The Layer 2 advice classifier is deliberately
excluded from that check: its output is a parsed boolean that never reaches judged text, so it can't
create the bias the guard exists to prevent.
`docs/adr/0011-classifier-routing-excluded-from-family-guard.md`.

**The semantic cache is partitioned by what the guardrail decided, not by how close two questions
sound.** Cosine distance encodes what a question is about far more strongly than whether it's asking
for a fact or asking for advice about that same fact: "does my employer need E-Verify" and "should I
switch to an E-Verify employer" measured 0.0885 apart, well inside the cache's 0.15 threshold. No
threshold separates that pair from a genuine paraphrase. The cache now only serves a cached response
whose stored classification matches the incoming question's classification, so a cached factual answer
can never be served to an advice-seeking question wearing similar words.
`docs/adr/0010-cache-classification-partition.md`.

**A citation format is part of the prompt contract, and a model swap can break it silently.** Swapping
the generator to `gpt-oss:120b` caused 5 of 21 golden rows to come back blocked, not because the
answers were ungrounded but because that model cites in its own training-format markup, `【1†...】`,
instead of this project's `[1]` convention. The fix normalizes that markup to `[N]` before verification
runs and reinforces the prompt; verification itself never loosened.
`docs/adr/0012-citation-markup-normalization.md`.

**Production embeds in-process, from the same GGUF file Ollama itself uses.** Neither Ollama Cloud nor
NVIDIA serves `nomic-embed-text`, and the 221 vectors already stored in `documents` were built by real
local Ollama, so switching models would mean re-deriving `NO_ANSWER_MAX_DISTANCE` against a threshold
that already sits knife-edge (0.4370 for a real off-topic control against a 0.50 cutoff). `GGUFEmbedder`
(`services/orchestrator/app/providers/embeddings.py`) loads the identical GGUF file via
`llama-cpp-python` and calls it with the same batch configuration Ollama's own engine uses
internally. That last part is the finding that makes this safe rather than merely plausible:
llama.cpp's default `n_batch` of 512 splits longer inputs and returns different vectors, and against
25 real stored vectors it produced 5 below cosine 0.9999, the worst at 0.974. The boundary is exact,
not approximate. Every chunk of 485 tokens or fewer matched; every chunk of 583 or more diverged.
`docs/adr/0013-in-process-gguf-embeddings.md`.

## Running it locally

You need Docker Desktop and a local Ollama with `nomic-embed-text` and `qwen3.5-8k:latest` pulled:

```
ollama pull nomic-embed-text
ollama pull qwen3.5
ollama create qwen3.5-8k -f Modelfile
```

The last command builds `qwen3.5-8k:latest` from the `Modelfile` at the repository root, which is
just `qwen3.5` with `num_ctx` raised to 8192; the default tag's context window is too small for this
project's retrieval-heavy prompts, so there's no way to pull it ready-made.

Ollama runs on your machine, not in a container. The orchestrator reaches it through Docker's
`host.docker.internal` hostname.

1. Copy the environment file: `cp .env.example .env`. None of the defaults are real secrets.
2. Start Postgres, the orchestrator, Redis, the gateway, and the observability stack:
   `docker compose up -d --build`. `infra/sql/init.sql` runs automatically the first time, as a
   Postgres init script.
3. Load the corpus: `docker compose run --rm orchestrator python -m app.ingest`. This fetches the 14
   pages in `data/sources/sources.yaml`, snapshots each to `data/sources/raw/`, chunks it by section,
   embeds every chunk, and writes it to Postgres.
4. Ask it something, through the gateway on 8080, not the orchestrator's own 8000 directly:

   ```
   curl -s -X POST http://localhost:8080/v1/query \
     -H "Content-Type: application/json" \
     -d '{"question": "How long is the STEM OPT extension?"}'
   ```

5. Run the frontend against the gateway:

   ```
   cd services/frontend
   cp .env.local.example .env.local
   npm install
   npm run dev
   ```

6. Open Grafana at `http://localhost:3000` (or whatever `GRAFANA_HOST_PORT` you set) and look in
   Tempo, via Explore, for a trace. One trace spans both services: `office-hours-gateway` and
   `office-hours-orchestrator`, the latter carrying its own `retrieve` and `generate` spans
   underneath.

### Running the eval

`eval/README.md` has the full schema and both modes in detail. The short version:

```
# Full mode: real providers, real judge, all 21 golden rows, several minutes
docker compose exec -T orchestrator python -m eval.run

# CI mode: stub providers, the small fixture corpus, no network call, seconds
python -m eval.run --ci
```

Full mode needs `JUDGE_PROVIDER`, `JUDGE_BASE_URL`, `JUDGE_API_KEY`, and `JUDGE_MODEL` set (see
`.env.example`), and it refuses to run against anything but a hosted NVIDIA judge, since a judge from
the generator's own family would grade its own writing. CI mode is what
`.github/workflows/eval.yml` runs on every pull request, against `eval/baselines.json`'s recorded
baseline, not the full thresholds. Pushing that workflow and turning on branch protection for `main`
(Settings, Branches, require the `ci-invariant-gate` check) are both done by hand; nothing in this
repository does either for you.

## Deploying it

Nothing described here has actually been deployed yet (see "What's not done" below). This is the
order to do it in, by hand.

1. **Neon** (Postgres with pgvector). Create a project and get two connection strings from the
   dashboard: the DIRECT one and the POOLED one. Use the direct string to apply
   `infra/sql/init.sql` and run any other DDL; use the pooled string for the running app's
   `DATABASE_URL`. Neon's pooler doesn't support every session-level feature DDL can need, which is
   the whole reason the two strings exist separately.
2. **Upstash Redis**. Create a database and copy its `rediss://` connection string. The extra `s` is
   what turns TLS on for the gateway's token bucket.
3. **Ollama Cloud**. Create an account and an API key. This is the primary production generator,
   `gpt-oss:120b`.
4. **NVIDIA** (build.nvidia.com or integrate.api.nvidia.com). Create an API key. It serves two
   separate roles here: the fallback generator (`openai/gpt-oss-20b`), tried once if Ollama Cloud
   errors or times out, and the eval judge (`nvidia/nemotron-3.5-lightning-30b-a3b`), which never
   runs on the serving apps and only needs its key when you run the eval by hand or in CI.
5. **Grafana Cloud**. Create a free-tier stack and get the OTLP integration's base endpoint URL and
   its `Authorization: Basic <base64 of instanceID:token>` header from the stack's own OTLP
   integration page. Both backend services append `/v1/traces` themselves, so the value you copy in
   should be the base URL with nothing appended.
6. **Langfuse**. Create a free-tier project and get its host, public key, and secret key. Only the
   orchestrator uses this; the gateway has no LLM calls to trace.
7. **Fly.io**, two apps, both in `iad`. Fly app names are globally unique across all accounts, so
   pick your own and edit the `app =` line in both `infra/deploy/fly.orchestrator.toml` and
   `infra/deploy/fly.gateway.toml` to match before the first deploy.

   Before the orchestrator's `fly deploy`, get the `nomic-embed-text` GGUF file and put it at
   `services/orchestrator/models/nomic-embed-text-f16.gguf` -- the Dockerfile bakes whatever is in
   that directory into the image, and there is nothing else in this pipeline that fetches it for
   you. If you already have Ollama running locally with `nomic-embed-text` pulled, the file is
   already on your machine:

   ```
   ollama show nomic-embed-text --modelfile   # prints the FROM line: the real blob path
   cp <that path> services/orchestrator/models/nomic-embed-text-f16.gguf
   ```

   The model is Apache 2.0 licensed and published as a GGUF by its own authors (nomic-ai); Ollama's
   local blob store just happens to already have a copy if you've pulled it there. The file is
   gitignored (`services/orchestrator/models/*.gguf`) -- it never gets committed, and it has to be
   in place on whatever machine actually runs `fly deploy`.

   ```
   fly apps create office-hours-orchestrator-<handle>
   fly apps create office-hours-gateway-<handle>

   fly secrets set -a office-hours-orchestrator-<handle> \
     DATABASE_URL="postgresql://user:password@ep-xxxx-pooler.region.aws.neon.tech/officehours?sslmode=require" \
     OLLAMA_API_KEY="<ollama-cloud-key>" \
     LLM_FALLBACK_API_KEY="<nvidia-key>" \
     SESSION_HASH_SALT="<a random secret, same value on both apps>" \
     OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64 of instanceID:token>" \
     LANGFUSE_SECRET_KEY="sk-lf-<your-langfuse-secret>"

   fly secrets set -a office-hours-gateway-<handle> \
     REDIS_URL="rediss://default:<password>@<host>.upstash.io:6379" \
     SESSION_HASH_SALT="<the SAME random secret as above>" \
     OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64 of instanceID:token>"

   fly deploy services/orchestrator --config infra/deploy/fly.orchestrator.toml
   fly deploy services/gateway --config infra/deploy/fly.gateway.toml
   ```

   Deploy the orchestrator first, then point the gateway's `ORCHESTRATOR_URL` (in
   `infra/deploy/fly.gateway.toml`) at `http://<orchestrator-app-name>.internal:8000`, Fly's private
   networking, before the gateway's own deploy.
8. **Vercel**. Import the repository, and in Project Settings set Root Directory to
   `services/frontend`. This is a dashboard setting; no file in the repository can express it. Set
   `NEXT_PUBLIC_GATEWAY_URL` to the gateway's public Fly URL
   (`https://office-hours-gateway-<handle>.fly.dev`) under Environment Variables. Once Vercel gives
   you the deployed URL, put it in the `ALLOWED_ORIGINS` line of both `fly.*.toml` files and redeploy
   both: `ALLOWED_ORIGINS` is a plain `[env]` value, not a secret, so this is a file edit and a
   redeploy, not a `fly secrets set`.

### Every required environment variable

| Variable | Service | Expects | Secret |
| --- | --- | --- | --- |
| `DATABASE_URL` | orchestrator | Neon POOLED connection string | yes |
| `OLLAMA_API_KEY` | orchestrator | Ollama Cloud API key | yes |
| `LLM_FALLBACK_API_KEY` | orchestrator | NVIDIA API key, fallback generator | yes |
| `SESSION_HASH_SALT` | orchestrator, gateway | random secret, identical value on both apps | yes |
| `OTEL_EXPORTER_OTLP_HEADERS` | orchestrator, gateway | Grafana Cloud OTLP auth header, identical on both apps | yes |
| `LANGFUSE_SECRET_KEY` | orchestrator | Langfuse secret key | yes |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_HOST` | orchestrator | Langfuse public key and region host | no |
| `LLM_PROVIDER`, `LLM_MODEL`, `OLLAMA_BASE_URL`, `OLLAMA_CLOUD` | orchestrator | selects Ollama Cloud as the primary generator (`ollama`, `gpt-oss:120b`, `https://ollama.com`, `true`) | no |
| `LLM_FALLBACK_PROVIDER`, `LLM_FALLBACK_MODEL`, `LLM_FALLBACK_BASE_URL` | orchestrator | selects NVIDIA as the fallback generator | no |
| `LLM_TIMEOUT_SECONDS` | orchestrator | generator request timeout, seconds | no |
| `ALLOWED_ORIGINS` | orchestrator, gateway | the Vercel deployment URL, for CORS | no |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | orchestrator, gateway | Grafana Cloud OTLP base URL, no path appended | no |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | orchestrator, gateway | `http/protobuf` | no |
| `LOG_LEVEL` | orchestrator | log verbosity, `INFO` by default | no |
| `REDIS_URL` | gateway | Upstash Redis, `rediss://`, TLS | yes |
| `ORCHESTRATOR_URL` | gateway | the orchestrator app's Fly private-networking hostname | no |
| `RATE_LIMIT_BUCKET_CAPACITY`, `RATE_LIMIT_REFILL_PER_SECOND` | gateway | token bucket sizing | no |
| `UPSTREAM_TIMEOUT_SECONDS` | gateway | upstream call timeout, seconds | no |
| `TRUSTED_PROXY_CIDRS` | gateway | empty, unless a real reverse proxy sits in front of the gateway | no |
| `NEXT_PUBLIC_GATEWAY_URL` | frontend | the gateway's public Fly URL | no, but public: baked into the client bundle |
| `JUDGE_API_KEY` | eval, run by hand or in CI | NVIDIA API key for the judge; never set on either serving app | yes |
| `EMBED_PROVIDER`, `EMBED_MODEL`, `EMBED_GGUF_PATH`, `EMBED_GGUF_N_CTX`, `EMBED_GGUF_THREADS` | orchestrator | selects the in-process GGUF embedder (`gguf`, `nomic-embed-text`, the path the Dockerfile bakes the file to, `2048`, `1`) | no |

`EMBED_GGUF_PATH` names a file baked into the image at build time (see "Deploying it" step 7's build
args and `services/orchestrator/Dockerfile`), not a secret and not fetched over the network at
container start.

## What's not done

The corpus hasn't been migrated to Neon. Every eval number above ran against the local Postgres
container in `docker-compose.yml`, not a hosted database.

Grafana Cloud and Langfuse are wired into both services' code, but neither has been verified against
a real endpoint. `.env.example` and the `fly.*.toml` comments describe the exact variables; nobody
has pointed them at a live Grafana Cloud stack or Langfuse project and confirmed a trace actually
shows up there.

Nothing has been deployed. Fly, Vercel, Neon, and Upstash all need accounts created and the steps
above run by hand before any of this is reachable outside a local machine.

The golden set is 21 rows, hand-written by the repository owner. It's the standard every number in
this README is measured against, and growing it is ongoing, not finished.
