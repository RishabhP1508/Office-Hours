# Architecture

Office Hours answers questions about F-1, OPT, STEM OPT, and H-1B rules using only official U.S.
government sources, and cites the source for every claim. This file records the decisions that shape
the system and the reason for each one. It is the reference the build loop checks against, so it
states what was decided, not what might be nice later.

Scope note: decisions marked **[P0]** are live now. Decisions marked with a later phase are committed
but not yet built, and are recorded here so nothing about them gets re-litigated mid-build.

## Product boundary

### The system answers factual questions and refuses advice **[P4, boundary fixed now]**

It will say what the rule is and where the rule is written. It will not say what a person should do,
whether an application will be approved, or which status suits them. Advice-seeking questions get a
refusal that points to a DSO or a licensed immigration attorney.

Why: telling someone what to do about their immigration status is legal advice, and giving it without
a license harms the person asking. The line between "what does the rule say" and "what should I do" is
the product, not a disclaimer bolted onto it.

### Every answer carries a disclaimer **[P0]**

Unofficial, not legal advice, not affiliated with USCIS or DHS.

Why: someone arriving from a search result needs to know what they are reading before they act on it.

### The system says when it does not know **[P0]**

If retrieval returns nothing relevant, the answer is that the sources do not cover it. A weak or
off-topic chunk is never stretched into a confident answer.

Why: a confident wrong answer about a filing deadline can cost a person their status. Silence costs
them one more search. This is a separate failure mode from advice-refusal and gets its own test.

### No personal identifying information is stored **[P0]**

Queries are not persisted with identity. The Go gateway redacts PII in transit at Phase 7.

Why: the audience is a population with real exposure to consequences from a leak of who asked what.

## Grounding and citations

### Every factual claim maps to a retrieved chunk, checked programmatically **[P4]**

An answer containing a claim that maps to no retrieved chunk is blocked before it renders. The check
asks a mechanical question, "is this cited chunk in the set we actually retrieved", not a model
question.

Why: a model judging its own faithfulness is the same model that produced the error. A set membership
test cannot be talked into agreeing.

### Citations point at the resolved URL, not the manifest URL **[P0]**

Both are stored. `source_url` is what the manifest lists, `resolved_url` is where the fetch landed
after redirects.

Why: one manifest entry already redirects. `traveling-as-an-international-student` resolves to
`traveling-as-an-f-or-m-student`, and the page has been retitled to match. Citing the manifest URL
would send a reader through a redirect to a page whose title does not match the citation. Keeping both
means a later manifest cleanup can be verified rather than guessed at.

### Answers state both the current rule and its dated replacement **[P0 in the prompt, P4 enforced]**

Where the corpus holds a rule and a dated successor, the answer gives both with their dates. The live
case is the DHS fixed-period-of-admission final rule, effective Sept 15 2026, which changes the F-1
post-completion departure period from 60 days to 30.

Why: today, both answers are true, for different dates. Picking one silently is wrong for half the
people asking, and which half changes on a known date.

## Corpus and chunking

### Sources come from a manifest the user maintains **[P0]**

`data/sources/sources.yaml` holds 14 entries, each with a URL, a title, and a topic. Ingestion reads
only from it.

Why: which sources count as authoritative is an editorial judgment, and it stays with the person
accountable for the answers.

### Fetching is plain HTTP, no browser engine **[P0]**

`httpx` plus an HTML parser. Redirects followed, robots.txt honored, the crawl rate-limited.

Why: the prep step fetched all 14 pages over plain HTTP and every one returned full content. A
headless browser would add a large dependency and a slow start to solve a problem this corpus does not
have.

### Fetch, snapshot, and chunk are three separate steps **[P0]**

Ingestion fetches HTML, normalizes it to a markdown snapshot with a YAML frontmatter header, writes
that to `data/sources/raw/`, then chunks the snapshot. The chunker's input is always a snapshot file,
never a live URL.

Why: the chunker is the part most likely to break, and it is only testable if its input is a file on
disk that does not change between runs. This split is what lets the chunking test assert real behavior
against the real corpus instead of against a mock.

### Chunk by section on h2 through h4, never by character count **[P0]**

Each chunk stores the heading text verbatim and the heading level as found.

Why: a citation is only useful if it points at the rule, and a fixed-size window cuts mid-rule and
cites a sentence fragment. All three levels are needed together because the corpus does not agree with
itself. The USCIS pages carry their real section boundaries in h4 with no h2 at all: the cap-gap
extension page has 11 h4 headings and zero h2, and specialty-occupations and electronic-registration
are the same shape. Chunking on h2 alone would collapse each of those pages into a single chunk.

### Parenting comes from document order and empty headings, not from level number **[P0]**

A heading immediately followed by another heading, with no body text between them, is a group label. A
chunk's parent is the nearest preceding group label. The level number is recorded but is not used to
decide who contains whom.

Why: the fixed-admission FAQ inverts the usual nesting. Its individual questions are h2 while the group
labels holding them (Transition Period, Understanding the Admit Until Date, Extensions of Stay,
Maintaining Status, Departure Period) are h3, sitting under h2 super-labels (GENERAL, F STUDENTS,
SCHOOL OFFICIALS). A stack keyed on level number pops the h3 group the moment the next h2 question
arrives, and mis-parents every answer on the page. The empty-heading rule reads the structure the page
actually has. The resulting breadcrumb is prefixed onto the chunk text so that both the embedding and
the model see which group a question belongs to.

### Content above the first heading is captured, never dropped **[P0]**

The pre-heading body becomes a chunk attributed to the page title.

Why: the H-1B electronic registration page carries roughly 65 lines above its first heading, including
the FY2021 to FY2026 registration and selection table. A chunker that starts at the first heading loses
the only place in the corpus that has those numbers.

### `fetched_at` and `last_verified_at` are separate columns **[P0]**

`fetched_at` is when the content was last downloaded. `last_verified_at` is when it was last checked,
whether or not it changed. A re-crawl that finds no change updates `last_verified_at` alone.

Why: collapsing them makes a page that was checked this morning and found unchanged report as months
stale, which is the opposite of the signal Phase 5 needs.

## Retrieval

### PostgreSQL with pgvector, no dedicated vector database **[P0]**

One Postgres instance holds the chunks, the embeddings, and later the keyword index.

Why: the corpus is 14 pages. A separate vector service would add an operational component, a second
consistency problem, and a second thing to deploy, in exchange for scale this project will not reach.

### Phase 0 uses a sequential cosine scan with no index **[P0]**

`ORDER BY embedding <=> $1 LIMIT k` over the whole table. The HNSW index (`vector_cosine_ops`) and the
`tsvector` keyword column arrive in Phase 3.

Why: exact search over a few hundred rows is fast, and it gives an exact ground truth for retrieval
quality. An approximate index now would introduce recall loss before there is any measurement in place
to notice it.

### Hybrid search fuses semantic and keyword ranks with RRF in one CTE **[P3]**

Reciprocal Rank Fusion over a pgvector ranking and a `tsvector` ranking, in a single query.

Why: these questions mix meaning ("can I work while my extension is pending") with exact tokens
("I-765", "cap-gap", "24-month"). Vector search alone misses the tokens. RRF combines ranks rather than
scores, so it needs no score normalization between two systems whose scores are not comparable. One CTE
keeps it a single round trip. Recorded in `docs/adr/0001-rrf-vs-weighted-blend.md` when Phase 3 lands.

## Models and providers

### LLM and embedding providers sit behind interfaces **[P0]**

Two interfaces, each with a local Ollama implementation and a hosted implementation.

Why: development runs on a machine with a GPU and production runs on a host without one. The provider
is the only thing that differs, so it should be the only thing that has to change.

### Dev defaults to local Ollama: `qwen3.5-8k:latest` to generate, `nomic-embed-text` to embed **[P0]**

Both are already pulled on the dev machine. `nomic-embed-text` produces 768-dimension vectors, which
fixes the column at `vector(768)`. `qwen3.5-8k:latest` is a reasoning model, run with thinking
disabled (`OLLAMA_THINK=false`, sent as `"think": false` on every `/api/chat` call): on a meaningful
fraction of questions it otherwise spends its entire output budget inside the thinking block and gets
cut off before writing any content, returning HTTP 200 with an empty answer.

Why: iteration during the build should cost nothing per query and should not depend on a network. A
grounded answer assembled from retrieved context does not need chain-of-thought, and here the
reasoning was actively destroying answers rather than improving them.

### The orchestrator container reaches Ollama on the host **[P0]**

`OLLAMA_BASE_URL` defaults to `http://host.docker.internal:11434`.

Why: the model weights and the GPU are on the Windows host. Running Ollama inside the compose stack
would mean re-downloading tens of gigabytes and losing GPU access.

### The corpus and the query always use the same embedding model **[P0]**

Ingestion and query read the same `EMBED_MODEL` environment variable.

Why: two embedding models place the same sentence in two different spaces, and cosine distance across
them is noise that looks like a result. A dimension change surfaces immediately as a Postgres error,
but a same-dimension model swap would not, which is why the setting is single-sourced rather than
configured twice.

### The eval judge is a hosted model from a different family than the generator **[P1]**

Where a check can be exact, it is programmatic instead of judged.

Why: a model scoring its own output prefers its own output. Every check moved from judgment to
mechanism is a check that cannot drift.

### No fine-tuning **[all phases]**

Why: these rules change on published dates. Retrieval picks up a corpus change the day it is ingested.
A fine-tuned model has to be retrained to forget the old rule, and until it is, it states the old rule
confidently.

## Services

### Python orchestrator now, Go gateway at Phase 7 **[P0, P7]**

Until Phase 7, rate limiting and caching live in FastAPI. At Phase 7 the gateway takes over the edge: a
Redis token bucket that returns 429 immediately on an empty bucket, `context.WithTimeout(ctx, 15s)` on
upstream calls, PII redaction, and trace propagation. Never `time.Sleep` for a retry.

Why: the orchestrator's work is IO-bound calls to a model and a database, where Python's ecosystem is
the reason to be there. The gateway's work is concurrent connection handling under a limit, where Go's
is. Sleeping to retry holds a goroutine and turns a slow upstream into an outage, and an empty bucket
is already a known answer, so it should be returned rather than waited on. Recorded in
`docs/adr/0003-go-python-split.md` at Phase 7.

### Every service has a Dockerfile, and all config comes from environment variables **[P0]**

Secrets live in a gitignored `.env`. `.env.example` documents every required variable.

Why: the repository is public and pushed by hand. A checked-in credential is not made safe by deleting
it in a later commit.

## Observability

### OpenTelemetry, exported to the `grafana/otel-lgtm` all-in-one container in dev **[P0]**

Phase 0 emits distinct `retrieve` and `generate` spans. Langfuse handles LLM-level traces. No
Elasticsearch, no ClickHouse, no standalone Prometheus, no Thanos.

Why: the questions worth asking are "which step was slow" and "what did retrieval hand the model", and
both need the steps separated inside one trace. One container gives Tempo, Loki, Prometheus, and
Grafana behind a single dependency, which is the right size for a dev stack.

## The eval gate

### Functional and safety checks are hard gates; quality scores are reported, never chased **[P1 onward]**

Hard gates: HTTP status, an uncited claim blocked from rendering, an advice query refused, 429 on an
empty bucket, one trace spanning gateway and orchestrator, tests passing. Reported and not gated:
faithfulness, answer relevancy, context precision, false-refusal rate, advice-leakage rate,
comprehensibility, and judge scores.

Why: a hard gate is a fact about behavior, and behavior is what has to be right. A score is a
measurement, and a measurement that gets optimized against stops measuring. When a score is low the fix
belongs in retrieval, the prompt, or the guardrail, never in the golden set, the threshold, the rubric,
or the metric.

### The golden set is hand-written by the user **[user]**

The build produces the schema, the loader, and template rows. It never authors or edits a
`ground_truth_answer`.

Why: the golden set is the standard the system is measured against. A standard written by the thing
being measured is not a standard.

### Golden rows carry `verified_on` and `volatility` **[P0 schema, P1 use]**

Why: a row can fail because retrieval regressed, or because the rule changed underneath it. The DHS
fixed-admission rule takes effect Sept 15 2026 and moves the departure period from 60 days to 30, so
some correct answers in this corpus have an expiry date. Marking volatility lets a labeled subset be
re-verified instead of the whole file.

## Deliberately not decided yet

- The hosted LLM and embedding providers for production. The interfaces are in place; the choice gets
  made when the eval gate runs against production in Phase 8.
- Whether the embedding model name becomes a column on `documents`. Phase 0 single-sources it through
  an environment variable. If Phase 3 needs to compare two embedding models over one corpus, that
  column is how to do it.
- Where the backend is hosted. `infra/deploy/` gets its file in Phase 8.
