# Office Hours

Office Hours answers factual questions about F-1, OPT, STEM OPT, and H-1B rules using only official
U.S. government sources, and cites the source for every claim. It does not give legal advice: it
tells you what a rule says and where it is written, and it refuses questions that ask what you
personally should do, redirecting you to your DSO or a licensed immigration attorney.

This is an unofficial tool, not affiliated with USCIS or DHS. Every answer carries that disclaimer.
No queries are stored with any identifying information.

## What's built so far (Phase 5)

A question goes through five checks. If it is too vague to search against, the system asks one
clarifying question and never touches the database. A classifier then decides whether the question
asks what the rules are or asks the system to make the asker's decision for them. Either way the
question is retrieved for, using hybrid search over a Postgres/pgvector corpus of 14 USCIS, Study in
the States, and ICE pages: a vector arm and a keyword arm fused with Reciprocal Rank Fusion in one
SQL query. If nothing retrieved is close enough, the system says its sources do not cover the
question and never calls the generator. The generator then answers from the retrieved passages, and
before anything renders, every bracket citation is checked against what was actually retrieved. A
citation pointing at a passage that does not exist blocks the answer entirely.

Every response carries one of five labels saying which of those paths it took, so the eval reads the
pipeline's own decision instead of guessing from the prose. The retrieve and generate steps each
produce their own OpenTelemetry span, visible as one trace in Grafana.

Phase 5 added the re-crawl job described under "Staying current" below, and freshness data on every
answer. Still to come: the Next.js frontend (Phase 6), the Go gateway with Redis rate limiting
(Phase 7), and deployment (Phase 8).

## Running it

You need Docker Desktop and a local Ollama with `nomic-embed-text` and `qwen3.5-8k:latest` pulled
(`ollama pull nomic-embed-text && ollama pull qwen3.5-8k`). Ollama runs on your machine, not in a
container; the orchestrator reaches it through Docker's `host.docker.internal` hostname.

1. Copy the environment file and adjust it if you need to:

   ```
   cp .env.example .env
   ```

2. Start Postgres, the orchestrator, and the observability stack:

   ```
   docker compose up -d --build
   ```

3. Load the corpus. This fetches the 14 pages listed in `data/sources/sources.yaml`, converts each
   to a markdown snapshot in `data/sources/raw/`, chunks it by section, embeds every chunk, and
   writes it to Postgres:

   ```
   docker compose run --rm orchestrator python -m app.ingest
   ```

4. Ask it something:

   ```
   curl -s -X POST http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -d '{"question": "How long is the STEM OPT extension?"}'
   ```

5. Open Grafana at [http://localhost:3000](http://localhost:3000) (or whatever port you set
   `GRAFANA_HOST_PORT` to, if something else on your machine already holds 3000) and look in Tempo
   (via Explore) for a trace from `office-hours-orchestrator`. Each query produces one trace with a
   `retrieve` span and a `generate` span.

Run the orchestrator's tests inside the container, since the host's Python version doesn't match
what the image builds against:

```
docker compose run --rm orchestrator pytest tests/test_chunking.py -v
```

Some tests insert and delete rows in `documents`, so point `DATABASE_URL` at a scratch database
before running the whole suite. Create one once, apply the schema, and load the small committed
fixture corpus into it:

```
export TEST_DB="postgresql://officehours:officehours@postgres:5432/officehours_fixtures"

docker compose exec postgres psql -U officehours -d postgres -c "CREATE DATABASE officehours_fixtures;"

# infra/ is not mounted into the orchestrator container, so apply the schema from the host:
docker exec -i office-hours-postgres-1 psql -U officehours -d officehours_fixtures -q < infra/sql/init.sql

docker compose run --rm -e DATABASE_URL="$TEST_DB" \
  -e INGEST_MODE=snapshot -e RAW_SNAPSHOT_DIR=/app/eval/fixtures/sources -e EMBED_PROVIDER=stub \
  orchestrator python -m app.ingest

docker compose run --rm -e DATABASE_URL="$TEST_DB" \
  -e EMBED_PROVIDER=stub -e LLM_PROVIDER=stub -e NO_ANSWER_MAX_DISTANCE=2.0 \
  -e RAW_SNAPSHOT_DIR=/app/eval/fixtures/sources \
  orchestrator pytest -m "not full_corpus" -q
```

That last command prints `96 passed, 6 skipped, 11 deselected`. The 6 skips are the refresh-graph
tests, which need the `[freshness]` extra the service image does not install.

If you forget and run against the real corpus, the write tests refuse to run and tell you so, rather
than writing to it. That guard fires when the target database holds a row for every URL in
`data/sources/sources.yaml`. Tests marked `full_corpus` need the real 14-source corpus and a
reachable Ollama, and are excluded above; the CI gate excludes them too.

## Required environment variables

All of these are documented with dev defaults in `.env.example`. None of them are secrets in dev;
if you point `LLM_PROVIDER` or `EMBED_PROVIDER` at a hosted API later, put the API key in `.env` and
never commit it.

| Variable | Purpose |
| --- | --- |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Postgres credentials for the `postgres` container. |
| `DATABASE_URL` | Full connection string the orchestrator uses. |
| `OLLAMA_BASE_URL` | Where the orchestrator reaches Ollama on the host. |
| `EMBED_PROVIDER`, `EMBED_MODEL`, `EMBED_DIM` | Which embedder to use. Ingestion and query always read the same `EMBED_MODEL`, so the corpus and a live query can never land in two different embedding spaces. |
| `LLM_PROVIDER`, `LLM_MODEL` | Which LLM to use to generate answers. |
| `RETRIEVAL_TOP_K` | How many chunks to retrieve per query. |
| `RRF_K`, `HYBRID_CANDIDATE_POOL` | Reciprocal Rank Fusion tuning for hybrid retrieval: `RRF_K` is the rank-fusion constant, `HYBRID_CANDIDATE_POOL` is how deep each arm (semantic, keyword) looks before fusion. Neither changes how many chunks the generator sees; that's still `RETRIEVAL_TOP_K`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | Where traces go and what service name they're tagged with. |
| `GRAFANA_HOST_PORT` | Host port Grafana is published on, default 3000. Override it if something else on your machine already holds that port. |
| `CRAWL_DELAY_SECONDS`, `USER_AGENT` | How politely `ingest.py` crawls the source pages. |
| `INGEST_MODE` | `fetch` (default) reads the manifest and fetches live URLs. `snapshot` skips the network and chunks whatever `.md` snapshots are already in `RAW_SNAPSHOT_DIR` -- used only by the CI eval gate to ingest `eval/fixtures/sources/`. |
| `ALLOWED_ORIGINS` | Comma-separated origins the orchestrator answers CORS preflight for. The Next.js frontend (`services/frontend`) calls the orchestrator directly until the Phase 7 gateway sits in front of it. |

## How retrieval works right now

Postgres with the pgvector extension holds every chunk, its embedding, and a generated `tsvector`
column in one `documents` table. A query runs two searches at once: a semantic arm over an HNSW
index (`embedding <=> $1`, cosine distance) and a keyword arm over a GIN index (`ts_rank_cd` against
the tsvector). Each arm ranks its own top candidates, and the two rank lists are fused with
Reciprocal Rank Fusion in a single SQL query, so a chunk that only one arm finds (an exact term like
"I-983" that means nothing to the embedding, or a paraphrase that shares no words with the source
text) still has a path into the answer. `docs/adr/0001-rrf-vs-weighted-blend.md` covers why ranks are
fused instead of raw scores.

Chunking happens by section (h2 through h4 headings), never by a fixed character count, so a
citation always points at a whole rule instead of a fragment. Some of these government pages nest
their headings in unusual ways: one FAQ page uses h2 for individual questions and h3 for the group
labels that contain them, and a couple of USCIS pages carry their only real section boundaries in
h4 with no h2 at all. The chunker in `services/orchestrator/app/ingest.py` reads a heading with no
body before the next heading as a group label rather than a content section, and assigns each
chunk's parent from that label chain in document order, not from comparing heading level numbers.

## Staying current

Government pages change, and some of these rules have a published date on which they change. The
re-crawl job in `services/orchestrator/app/recrawl.py` re-fetches every source in the manifest,
compares each page to the snapshot that was actually indexed, and decides whether the difference
matters:

```bash
pip install -e "./services/orchestrator[freshness]"
python -m app.recrawl --dry-run     # fetch and classify, write nothing
python -m app.recrawl               # apply the result
```

The comparison strips navigation, timestamps and boilerplate, then compares the remaining lines. An
identical page is `unchanged`. Reordering, or a difference that survives only as capitalization,
punctuation or markdown syntax, is `cosmetic`. Anything else is `meaningful`. The bias is
deliberate: any real addition or removal of a non-boilerplate line counts as meaningful, because a
re-index costs a few minutes and serving a stale rule costs someone their status. That also means a
typo fix triggers a re-index it did not need, and a rule change hidden inside a line the boilerplate
filter drops would be missed. `classify_change`'s docstring lists the failure modes.

An `unchanged` or `cosmetic` page updates `last_verified_at` and nothing else, so a page checked this
morning and found unchanged does not read as months stale. A `meaningful` page updates `fetched_at`
and `page_last_updated` too, and its chunks are re-embedded and replaced. The snapshot on disk is
only rewritten on a meaningful change, which keeps it equal to what is actually in the database.

The job is a LangGraph state machine, one graph run per source, checkpointed to SQLite. A run that
dies partway through resumes at the step it died on when restarted with the same run id, which
defaults to the current UTC date. One source failing its fetch does not stop the others: it retries
a bounded number of times, records `fetch_failed`, and the run continues.

LangGraph pulls in a large dependency tree, and none of it may reach the code that serves `/query`
or the code that runs the eval. It lives in its own `[freshness]` extra, `app/recrawl.py` imports it
inside the function that builds the graph rather than at module level, and two tests in
`services/orchestrator/tests/` assert that importing `app.main`, `app.pipeline`, `app.recrawl` or
`eval.run` pulls in no module whose name begins with `langgraph` or `langchain`. See
`docs/adr/0005-langgraph-for-the-refresh-pipeline.md`, which also says plainly what a plain Python
loop over 14 sources would have done just as well.

`.github/workflows/recrawl.yml` runs the job on a schedule. Like the eval workflow, pushing it is
yours to do by hand.

### Freshness on an answer

Every answer carries a `freshness` block: the date it was generated, and for each source it drew
on, when that page was last updated, last fetched, and last verified. Where a retrieved source
states a rule with a known effective date, the answer also says so in its own text, with the date
and a link. The live case is the DHS fixed-period-of-admission final rule, effective September 15
2026, which changes the F-1 post-completion departure period from 60 days to 30. Asking today about
the departure period returns the current 60-day rule and a sentence saying a rule takes effect on
September 15 2026, so the answer differs before and after that date.

Crawl dates stay out of the answer text on purpose. An effective date is stated by the source
itself, so putting it in the answer adds nothing the citations do not already support. When this
system last checked a page is a fact about this system, not about the source, so it belongs in the
structured field where the interface can show it.

## CI eval gate

`.github/workflows/eval.yml` defines two jobs. `ci-invariant-gate` runs on every pull request: it
spins up a fresh Postgres, ingests the small fixture corpus at `eval/fixtures/sources/` with
deterministic stub providers (`LLM_PROVIDER=stub`, `EMBED_PROVIDER=stub`, no network, no GPU, no
API key), and runs `python -m eval.run --ci`. That checks retrieval, citation mapping, refusal
bookkeeping, and error handling against `eval/baselines.json`'s recorded CI baseline. It does not
measure answer quality, and the run says so in its own banner. `full-eval` runs only on
`workflow_dispatch`, points at an orchestrator you already have running somewhere reachable, and
uses `secrets.JUDGE_API_KEY` to run the real eval against the hosted judge and RAGAS. See
`docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md` for why these are two separate gates
rather than one, and `eval/README.md` for the full metric breakdown.

Two things in this workflow are yours to do by hand; nothing in this repository does them for you:

1. **Push `.github/workflows/eval.yml`.** A workflow file only starts running once it exists on
   GitHub. Commit it and push like anything else in the repo.
2. **Turn on branch protection for `main` and mark the check required.** In the repository's
   Settings, under Branches, add a protection rule for `main` that requires the
   `ci-invariant-gate` status check to pass before merging. Until you do this, the workflow runs and
   reports a result, but nothing stops a pull request from merging on a red run.

If `JUDGE_API_KEY` should ever be needed for the `full-eval` job, add it under Settings, Secrets and
variables, Actions, as a repository secret. Never put it in a file that gets committed.

## Not legal advice

Everything this system says comes from the sources it cites. It does not know your specific
situation, it does not evaluate your case, and it should not be the basis for a decision about your
immigration status. Talk to your school's designated school official (DSO) or a licensed immigration
attorney for anything that depends on your circumstances.
