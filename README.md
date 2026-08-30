# Office Hours

Office Hours answers factual questions about F-1, OPT, STEM OPT, and H-1B rules using only official
U.S. government sources, and cites the source for every claim. It does not give legal advice: it
tells you what a rule says and where it is written, and it refuses questions that ask what you
personally should do, redirecting you to your DSO or a licensed immigration attorney.

This is an unofficial tool, not affiliated with USCIS or DHS. Every answer carries that disclaimer.
No queries are stored with any identifying information.

## What's built so far (Phase 0)

One question flows end to end: a query is embedded, matched against a Postgres/pgvector corpus of
14 USCIS, Study in the States, and ICE pages, answered by a local LLM grounded in the retrieved
passages, and returned with citations. The retrieve and generate steps each produce their own
OpenTelemetry span, visible as one trace in Grafana.

There is no guardrail classifier yet (advice detection, citation verification, and freshness
flagging land in Phase 4), no hybrid keyword search (Phase 3), and no eval CLI (Phase 1). The system
prompt carries the "answer only from context, cite it, say when the sources don't cover it" rules on
its own for now.

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
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | Where traces go and what service name they're tagged with. |
| `GRAFANA_HOST_PORT` | Host port Grafana is published on, default 3000. Override it if something else on your machine already holds that port. |
| `CRAWL_DELAY_SECONDS`, `USER_AGENT` | How politely `ingest.py` crawls the source pages. |

## How retrieval works right now

Postgres with the pgvector extension holds every chunk and its embedding in one `documents` table.
A query is embedded with the same model used at ingest time, and Postgres does a sequential cosine
scan (`ORDER BY embedding <=> $1 LIMIT k`) to find the closest chunks. There's no index yet: at a few
hundred rows, sequential scan is fast, and it gives an exact answer to measure retrieval quality
against before an approximate index (HNSW, arriving in Phase 3) could introduce recall loss no one
would notice yet.

Chunking happens by section (h2 through h4 headings), never by a fixed character count, so a
citation always points at a whole rule instead of a fragment. Some of these government pages nest
their headings in unusual ways: one FAQ page uses h2 for individual questions and h3 for the group
labels that contain them, and a couple of USCIS pages carry their only real section boundaries in
h4 with no h2 at all. The chunker in `services/orchestrator/app/ingest.py` reads a heading with no
body before the next heading as a group label rather than a content section, and assigns each
chunk's parent from that label chain in document order, not from comparing heading level numbers.

## Not legal advice

Everything this system says comes from the sources it cites. It does not know your specific
situation, it does not evaluate your case, and it should not be the basis for a decision about your
immigration status. Talk to your school's designated school official (DSO) or a licensed immigration
attorney for anything that depends on your circumstances.
