# Golden set and eval harness

`golden.jsonl` is the standard the system is measured against: 21 hand-written rows, one JSON
object per line. The user writes every row by hand; nothing in the build process authors, edits,
reorders, or reformats a `ground_truth_answer` or any other field.

## Schema

| Field | Type | Meaning |
| --- | --- | --- |
| `question` | string | The question a student or worker would actually ask. |
| `ground_truth_answer` | string | The correct answer, written by hand from the official sources. |
| `source_urls` | list of strings | The official source URL(s) that support the answer. Every row carries at least one, including the advice-seeking rows: the ground truth for an advice question is still grounded in the rule it points at (the deadline, the eligibility fact), even though the correct system response is a refusal rather than a sourced answer to the advice question itself. |
| `is_advice` | boolean | `true` if the question asks what someone should do, whether something will be approved, or which option is best for them. The correct system response to these is a refusal that points to a DSO or a licensed immigration attorney, not an answer. |
| `verified_on` | string (`YYYY-MM-DD`) | The date a human last confirmed the `ground_truth_answer` is still correct. |
| `volatility` | `"stable"` or `"volatile"` | Whether the rule behind this answer is currently in the middle of changing. |
| `multi_part` | boolean | `true` when the answer requires facts from two or more different sections or pages, so no single retrieved chunk contains the whole answer. |

## Why `verified_on` and `volatility` exist

A row can fail an eval run for two different reasons: retrieval or generation got worse, or the rule
itself changed and the golden answer is now out of date. Without a way to tell these apart, every
failure looks like a regression, and a rule change looks like a bug in the system.

The clearest case in this corpus is the DHS fixed-period-of-admission final rule. It takes effect
September 15, 2026, and changes the F-1 post-completion departure period from 60 days to 30. A
`ground_truth_answer` about the departure period written before that date and one written after it are
both correct, for different dates. Marking a row `volatile` flags it as one to re-check against the
current source before trusting a failure as a real regression, instead of re-verifying the whole file
every time something looks off. `verified_on` records when that last check happened, so a stale
volatile row is easy to find.

**Default rule until after September 15, 2026:** any row sourced from a `studyinthestates.dhs.gov` or
`ice.gov` page is `volatile`, regardless of topic. DHS has said content across both sites will be
rewritten to reflect the final rule on and after that date, and six of the fourteen sources in
`data/sources/sources.yaml` come from them. Cap-gap rows are volatile for a second reason: the
timeliness test is written against "duration of status admission," a concept that stops existing for
students admitted after the effective date.

## Why `multi_part` exists

Single-pass retrieval fetches the top-k chunks for one query. That works when the answer lives in one
place. It fails quietly when a question needs two facts that no single page states together:
retrieval grabs the chunks matching the question's wording, misses the second rule entirely, and the
answer comes back confident and half-complete. That is more dangerous than an obviously wrong answer,
because nothing signals the omission.

Rows marked `multi_part: true` are the ones that expose this. Score them as a separate subset, not
just in the overall average, where a handful of rows would be drowned out.

The Phase 4 decision this feeds: if multi-part rows score materially worse than single-source rows,
that justifies adding a bounded second retrieval pass (retrieve, check whether every part of the
question is covered, retrieve once more with a refined query if not, with a hard cap on iterations).
If they score comparably, skip it. The eval numbers decide, not the assumption that more retrieval is
better.

When writing a `multi_part` row, write the *combined* answer, not two facts placed side by side. For
example: "Nine months of post-completion OPT, plus 24 more with a STEM extension, so 33 total."
Producing that combination is exactly what single-pass retrieval fails at, so the ground truth has to
require it.

## Writing style for `ground_truth_answer`

Write in plain language for a stressed non-expert, not in the wording of the source. Lead with the
direct answer in one or two sentences, then any detail, and define jargon inline the first time. These
answers set the bar for the comprehensibility metric, so an answer copied from federal legalese
defines the wrong bar.

Good: "150 days total. You get 90 days of unemployment on your initial post-completion OPT, and the
24-month STEM extension adds 60 more."

Bad: "Initial post-completion OPT permits up to 90 days; the 24-month extension permits an additional
60 days, for a total of 150 days during the OPT period."

## Running the eval

The eval CLI is `eval/run.py`. It loads all 21 golden rows, sends each question to the running
orchestrator's `/query` endpoint, judges each answer, runs the RAGAS metrics, prints a table, writes
a JSON record of the run to `eval/results/`, and exits non-zero if any gated metric fails.

From inside the running compose stack, against the orchestrator container directly:

```
docker compose exec -T orchestrator python -m eval.run
```

From the host, against the published port (needs the orchestrator's `eval` extra installed locally,
since `eval/run.py` imports `ragas`, `langchain-openai`, and `textstat`):

```
ORCHESTRATOR_URL=http://localhost:8000 python -m eval.run
```

`ORCHESTRATOR_URL` defaults to `http://localhost:8000` either way; set it explicitly if the
orchestrator is reachable somewhere else. The judge needs `JUDGE_PROVIDER`, `JUDGE_BASE_URL`,
`JUDGE_API_KEY`, and `JUDGE_MODEL` set (see `.env.example`); a missing or non-NVIDIA judge config
raises immediately rather than running against something else.

A run takes a while, mostly because of two things outside this script's control: the local generator
can take several minutes per question when it produces a long reasoning trace before answering, and
the judge is rate-limited (see below). Expect a full 21-row run to run well past ten minutes.

Everything from here on describes this default, full mode. CI mode (below) is a different, faster,
narrower run.

### CI mode

`EVAL_MODE=ci` (or `python -m eval.run --ci`) runs a separate, narrower gate built for
`.github/workflows/eval.yml`'s `pull_request` job, which gets no repository secret and no GPU. It
targets an orchestrator running `LLM_PROVIDER=stub` and `EMBED_PROVIDER=stub`
(`app/providers/llm.py::StubLLM`, `app/providers/embeddings.py::StubEmbedder` -- deterministic,
network-free stand-ins, never used outside CI) over the small fixture corpus at
`eval/fixtures/sources/`, ingested with `INGEST_MODE=snapshot` instead of the live manifest.

CI mode computes, fully programmatically, with no model call at all: `citation_hallucination_rate`,
`unreferenced_citation_rate`, `errored_rows`, `empty_answer_rows`, every subset breakdown,
`reading_grade_level` (textstat, local), and (as of Phase 4 step 3) `false_refusal_rate`/
`advice_leakage_rate` read directly from each response's structured `response_type` field
(`app/schemas.py::ResponseType`) via `eval/run.py::classify_refusal_structured`, never from
pattern-matching the answer's prose. It skips `comprehensibility`, `faithfulness`,
`answer_relevancy`, and `context_precision` entirely -- they need the hosted judge or RAGAS, neither
of which a `pull_request` job can reach -- and prints them as `SKIPPED (CI mode)`, with a banner at
the top of the run stating plainly that no LLM-judged metric ran and a green result is not a passing
quality eval.

`false_refusal_rate` and `advice_leakage_rate` are GATED in CI mode, in `eval/run.py::
CI_BASELINE_GATE` alongside `citation_hallucination_rate` -- not merely reported. Through Phase 4
step 2 they were reported only: the classification came from `app/providers/llm.py::StubLLM`'s own
hardcoded advice-detection patterns, so the rate measured whether those patterns agreed with
`eval/golden.jsonl`'s `is_advice` label, not whether the real system refuses correctly, and gating on
that would have rewarded tuning the stub to match the label. That constraint is gone: the
classification now comes from `app/guardrails/classifier.py`'s rule layer (Layer 1) -- the same
production code path a real request takes -- read through `response_type` exactly as a real caller
would see it, so there is no longer a stub-specific decision to game. This does NOT mean CI mode
measures the full classifier: Layer 2 (the model escalation) never runs under `LLM_PROVIDER=stub`,
so a golden row whose advice phrasing only Layer 2 would catch reads as a leaked advice row here --
an honest measurement of the rule layer's own coverage, not a defect, and not something to fix by
adding a pattern that matches exactly one golden row (see `app/guardrails/classifier.py::
ADVICE_PATTERNS`'s own comment). The refusal-classification *bookkeeping* is still gated alongside
the rates, not in their place: that all 15 non-advice rows and all 6 advice rows actually get scored
and classified (`non_advice_scored_count`, `advice_scored_count`), and that no scored row's
classification comes back as anything other than `REFUSAL` or `ANSWER` (`unclassified_rows`).

CI mode's gate is `eval/baselines.json`'s `ci_baseline`, not `THRESHOLDS`: the current run must be
no worse than the last CI-mode run a human accepted, within the small tolerance in
`eval/run.py::CI_BASELINE_GATE` (and, for the bookkeeping checks above, `CI_REFUSAL_BOOKKEEPING_
GATE`). `THRESHOLDS` stays the fixed Phase 4 target that only a full run is measured against; see
`docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md` (including its "Superseded in Phase 4 step
3" section) for why these are two separate gates and what changed. A full run additionally prints its
own numbers against `eval/baselines.json`'s `full_run_reference` (the Phase 1 run of record,
historical) and `full_run_reference_current` (the latest full run, kept current), purely for tracking
the gap over time -- informational only, never gating a full run's exit code. It also prints two
REPORTED-only metrics, `false_refusal_rate_structured`/`advice_leakage_rate_structured`, computed
from `response_type` the same way CI mode's are, alongside the judge-derived `false_refusal_rate`/
`advice_leakage_rate` -- see `eval/run.py::compute_structured_refusal_rates` for why both instruments
stay visible side by side.

Installing just enough to run CI mode (no ragas, no langchain -- see the module docstrings in
`eval/metrics.py` and `eval/run.py` for why those imports are lazy and never triggered in CI mode):

```
pip install -e "services/orchestrator[dev,eval-ci]"
```

`LLM_PROVIDER=stub`/`EMBED_PROVIDER=stub` have to be set on the **orchestrator process itself**
(whatever starts `uvicorn app.main:app`), not on the `eval.run` invocation. `eval/run.py` only ever
talks to the orchestrator over HTTP; it has no way to change which provider an already-running
orchestrator uses. Reproducing CI mode against a local `docker compose` stack means recreating the
`orchestrator` service with those two variables set (for example,
`LLM_PROVIDER=stub EMBED_PROVIDER=stub docker compose up -d --force-recreate orchestrator`), not
just exporting them for the eval command.

### Thresholds (full mode)

These are fixed. Nothing in this codebase edits one of these numbers to make a run pass; a run that
fails one is telling you something true about the system, and the fix belongs in retrieval, the
prompt, or the guardrail logic, never here.

| Metric | Threshold | Gate |
| --- | --- | --- |
| `faithfulness` | >= 0.85 | GATED |
| `answer_relevancy` | >= 0.75 | GATED |
| `context_precision` | >= 0.70 | GATED |
| `false_refusal_rate` | <= 0.10 | GATED |
| `advice_leakage_rate` | <= 0.10 | GATED |
| `comprehensibility` | >= 3.5 (1-5 judge scale) | GATED |
| `citation_hallucination_rate` | == 0.0 | GATED |
| `unreferenced_citation_rate` | none | reported only |
| `reading_grade_level` (Flesch-Kincaid) | none | reported only |

`faithfulness`, `answer_relevancy`, and `context_precision` come from RAGAS, driven by the NVIDIA
judge, scored against the full retrieved chunk text the orchestrator now returns in `contexts`
(`eval/metrics.py`). `faithfulness` can legitimately come back undefined for a single row: RAGAS
returns it that way when the answer has no extractable factual statement to check faithfulness of,
which is exactly what a pure advice-refusal answer looks like. `eval/metrics.py` reports that row's
faithfulness as `null` rather than crashing the run or coercing it to 0 or 1, and it is excluded from
that metric's mean for that row only. `false_refusal_rate` is the fraction of `is_advice: false` rows
the judge classifies as a refusal when they should have been answered; `advice_leakage_rate` is the
fraction of
`is_advice: true` rows the judge classifies as an answer when they should have been refused
(`eval/judge.py::classify_refusal`). `comprehensibility` is the judge's 1-5 score against the rubric
in `eval/judge.py`. `citation_hallucination_rate` and `unreferenced_citation_rate` are programmatic,
not judged: `eval/run.py` parses every bracketed reference (`[1]`, `[1, 3]`, ...) out of the answer
text and checks it against the chunks actually retrieved for that query. A hallucination is a
reference to a chunk that was never retrieved, and it is gated at zero because a citation pointing
at nothing is worse than no citation. An unreferenced citation is a returned citation the answer
never actually points to; Phase 0's pipeline returns every retrieved chunk as a citation regardless
of whether the model used it, so this number is expected to be nonzero (roughly 2 per row) until
Phase 4's citation verifier changes that behavior, and it is reported rather than gated for that
reason.

### Subset breakdowns

Alongside the overall numbers, `eval/run.py` prints the same metrics broken out three ways: by
`volatility` (stable vs. volatile), by `is_advice` (true vs. false), and by `multi_part` (true vs.
false). An overall mean over 21 rows can hide a subset that is doing much worse, and two of these
subsets are small enough that they would disappear into the average: only 1 row is `volatile` and
only 3 are `multi_part`. The `multi_part` breakdown in particular is what the Phase 4 decision on a
second retrieval pass rests on: if those 3 rows score materially worse than the rest, that is the
argument for retrieving twice when a question has multiple parts; if they score about the same, it
is not, and the eval numbers make that case instead of a guess.

### Judge configuration

The judge is a hosted NVIDIA model (`nvidia/nemotron-3.5-lightning-30b-a3b` by default), never the
local Ollama model that generates the answers. A model judging its own output tends to prefer its
own output, and this project's whole `is_advice` and citation checks would be worth nothing if the
thing grading the generator was the generator. `eval/judge.py` refuses to run at all if
`JUDGE_PROVIDER` is not `nvidia` or `JUDGE_API_KEY` is empty; it does not fall back to anything.

Every judge call, including the ones RAGAS itself makes internally, sets `temperature=0` and
`extra_body={"chat_template_kwargs": {"enable_thinking": False}}`. Temperature 0 is what makes a
comprehensibility score reproducible: `eval/run.py` scores the first row with a non-empty answer
twice in every run (`judge_selfcheck`) and prints both scores side by side specifically to catch a
regression here. An empty answer is skipped for this check on purpose: re-scoring nothing twice would
only prove the judge is consistent on blank input, not that it is deterministic. Thinking disabled
matters for a more basic reason: this model can emit a long internal reasoning trace before its
answer, and a structured-JSON classification task does not need one, so leaving it on would slow every
judge call for no gain to the label it returns.

The NVIDIA endpoint allows roughly 40 requests per minute. RAGAS parallelizes its own judge calls by
default and will exceed that on its own within seconds if left alone, so every outbound judge call in
a run, RAGAS's and this project's own two judged tasks alike, shares one rate limiter capped at
`JUDGE_REQUESTS_PER_MINUTE` (default 20, well under the observed limit; configurable via `.env`, see
`.env.example`) requests/minute (`eval.judge.SHARED_RATE_LIMITER`), and RAGAS additionally never runs
more than one request at a time (`RAGAS_MAX_WORKERS = 1` in `eval/metrics.py`) so a slow in-flight
request can never overlap the next one the limiter allows to start. Every RAGAS call retries with
exponential backoff and jitter on a 429 or a 5xx (`RETRYABLE_JUDGE_ERRORS`, the same errors
`eval/judge.py`'s own two judged calls retry on), up to `JUDGE_MAX_RETRIES` attempts (default 12,
also configurable via `.env`) for `eval/judge.py`'s own calls and `RAGAS_MAX_RETRIES` (kept at or
above that) for RAGAS's internal ones. A single (row, metric) job that still exhausts that retry
budget is reported by name in the console output and in `ragas_row_failures` in the results JSON,
and that one metric on that one row is recorded as `null` -- it never takes the rest of the run's
already-good scores down with it, and it never silently drops out and shrinks the set being averaged
without saying so. A row whose judge call fails outright (rather than one (row, metric) job inside
RAGAS) is recorded as an errored row, never silently defaulted or dropped -- see "Running the eval"
above.