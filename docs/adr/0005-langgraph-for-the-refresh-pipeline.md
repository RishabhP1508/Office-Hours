# 0005: LangGraph for the refresh pipeline, isolated behind an optional extra

## Context

Phase 5 has to keep the corpus current. For each of the 14 sources in `data/sources/sources.yaml`,
that means fetch the page, compare it to the snapshot we last indexed, decide whether the difference
matters, and then either record that we checked it or re-chunk, re-embed and re-index it. A fetch can
fail, and one source failing must not stop the other 13.

The timing is not hypothetical. The DHS fixed-period-of-admission final rule takes effect on
2026-09-15, and DHS has said content across studyinthestates.dhs.gov and ice.gov will be updated on
and after that date. Six of the 14 sources are on those two domains, so roughly 43 percent of the
corpus is scheduled to be rewritten by the publisher within days of this code landing, including the
F-1 post-completion departure period changing from 60 days to 30.

## Decision

Model the per-source refresh as an explicit state machine in LangGraph, in
`services/orchestrator/app/recrawl.py`, with its own entry point (`python -m app.recrawl`) and its
own optional dependency extra (`[freshness]`).

The graph is: `fetch` to `diff` to a router on the classification, then either `verify_only` for an
unchanged or cosmetic page, or `chunk`, `embed`, `reindex` for a meaningful one. A failed fetch
retries up to a bounded attempt count and then routes to `record_failure`. It is compiled with an
`AsyncSqliteSaver` checkpointer, and each source gets its own thread, keyed `run_id:source_url`.

The isolation is part of the decision, not a detail of it. `langgraph` must never reach the serving
path (`app/main.py`, `app/pipeline.py`) or the eval path (`eval/run.py`), directly or transitively.
The `[freshness]` extra is installed only by `.github/workflows/recrawl.yml` and by a developer
running the job by hand. The service image does not install that extra, though see the Tradeoff
section for why that is not the same as the service image being free of langgraph today. `app/recrawl.py` imports `langgraph`
inside the function that builds the graph rather than at module scope, so the change classification
and the database bookkeeping are importable and testable without it, which is what lets the CI
invariant gate exercise the refresh logic on the lightweight dependency set. Two guard tests hold the
line: the existing one asserting `eval.run` never pulls ragas or langchain, and a new one asserting
that importing `app.main` or `app.pipeline` leaves no module in `sys.modules` whose top level name
begins with `langgraph` or `langchain`. The second one scans by prefix rather than checking a fixed
list of names, so a future `langgraph_anything` is caught by the test that already exists.

## What the graph actually buys

**Resumable checkpointing.** A run that dies partway through resumes at the node it died on instead
of starting over. Re-embedding a source costs real time against a local Ollama, and re-fetching costs
goodwill with a government web server that is being crawled on a schedule.

**Retry with per-source state.** The attempt count lives in the source's own state, not in a local
variable in a loop, so it survives the process dying and is inspectable afterward.

**Routing as data.** The unchanged, cosmetic and meaningful branches are edges in a graph rather than
nested conditionals, so which path a source took is a fact recorded in the checkpoint rather than a
line of stdout that scrolled past.

**Inspectable state.** `graph.aget_state(config)` answers "what happened to this source, and what was
it about to do next" after the run is over, without re-running anything.

## Tradeoff

**A plain Python loop over 14 sources would also have worked.** That is worth saying plainly rather
than burying. Fourteen iterations, a try/except, a retry counter and a small JSON file recording
which sources finished would deliver every behavior in the Definition of Done for this phase, in
substantially less code, with no new dependency at all. Anyone reading `app/recrawl.py` and thinking
"this is more machinery than 14 URLs need" is right about today.

**The dependency is not small, and the honest accounting is more awkward than it first looks.**
Installing the `[freshness]` extra into a clean `python:3.12-slim` pulls 41 distributions and about
83 MB of site-packages, including `langchain-core`, `langsmith`, `requests`, `websockets`,
`zstandard` and `sqlite-vec`. That is the number that matters for the CI invariant gate, whose
`[dev,eval-ci]` set is just `openai` and `textstat`, and for a future production image that stops
installing the eval harness.

It is not, however, the marginal cost in the image built today. `langgraph` 1.2.11 is **already
installed** in the orchestrator image, and has been since Phase 1, because the `eval` extra pins
`langchain==1.3.18` and that release requires `langgraph<1.3.0,>=1.2.11`. The Dockerfile installs
`[dev,eval]`, so the serving image ships langgraph whether or not this phase exists. Against that
baseline the `[freshness]` extra adds only `langgraph-checkpoint-sqlite`, `sqlite-vec` and
`aiosqlite`.

Two things follow, and both are worth stating rather than glossing. First, the phrase "the serving
path pays nothing for it" is true about **imports**, not about **installed bytes**: the guard test
proves `app/main.py` and `app/pipeline.py` import none of it, and that is the property being
defended. Second, the reason the serving image carries a graph framework at all is a pre-existing
wart this ADR did not create and does not fix: eval-only dependencies are installed into the service
image, which `services/orchestrator/pyproject.toml` already flags in the `eval` extra's own comment.
Anyone claiming this phase introduced langgraph to the service image would be wrong; anyone claiming
the service image is free of it would also be wrong.

**The isolation is only as strong as the guard test.** Nothing about a lazy import prevents someone
from adding `from app.recrawl import something` to `app/pipeline.py` later. The test is the
mechanism, not the file layout, which is why it asserts a whole class of module names rather than the
one that prompted it.

**Graph state has to be serializable.** Everything crossing a node boundary is checkpointed, so the
state carries ISO date strings and plain dicts rather than the `date` objects and dataclasses the
rest of the codebase uses. That is a small, permanent tax on readability at the graph boundary.

**Debugging is less direct.** A failure inside a node surfaces through the framework's execution
machinery rather than as a stack trace through the code you wrote. On a job that runs unattended on a
cron, that matters more than it would locally.

## The real reason for choosing it now

The corpus grows. Fourteen sources is where the failure modes of a refresh pipeline are cheap to
learn: a partial run, a source that 503s, a page that changes cosmetically every day because it
carries a timestamp, a page that changes meaningfully once a year. At 300 sources the same failures
are expensive, and that is the wrong moment to be designing the retry and resume semantics. Building
the state machine now means the September 15 rewrite is the first real test rather than the first
real design exercise.

The isolation is what makes this defensible rather than decorative. The serving path never imports
any of it, which is the property the guard test defends and the property that matters when
`/query` is on the hot path. The CI invariant gate installs none of it, which is why a pull request
still runs on `openai` plus `textstat`. If either of those ever stops holding, the argument for the
framework stops holding with it, and the guard test is what makes that visible instead of gradual.

## Alternatives considered

**A plain Python loop with a JSON progress file.** The honest baseline, covered above. Rejected for
the reasons in the previous section, not because it would not have worked.

**Celery, Airflow or Prefect.** All three are heavier than LangGraph here and all three want
infrastructure, a broker or a scheduler, that this project does not otherwise run. GitHub Actions
already provides the cron.

**Fold the refresh into `app/ingest.py`.** Tempting, since it already fetches, chunks and embeds, and
the refactor in this phase reuses its fetch path rather than copying it. Rejected because it would
put `langgraph` one import away from a module the serving path can reach, which is precisely the
thing this ADR is trying to prevent, and because ingest's job is "build the corpus from scratch"
while refresh's job is "decide what changed", which are different questions with different failure
modes.

**Use the checkpointer's Postgres backend instead of SQLite.** Rejected for now. It would put refresh
run state in the same database as the corpus, which sounds tidy but means a schema this project does
not own appearing in `infra/sql/`. SQLite keeps the job's own state in a file the job owns, and
nothing else reads it.
