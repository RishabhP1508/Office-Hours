"""Quality metrics: RAGAS (faithfulness, answer_relevancy, context_precision) and reading level.

RAGAS is driven by the same NVIDIA judge as eval/judge.py, through
LangchainLLMWrapper(ChatOpenAI(...)), never by the local generator (see ARCHITECTURE.md).
answer_relevancy also needs an embedding model to compare a judge-generated hypothetical question
against the real one; that uses the local Ollama nomic-embed-text model, not the generator, so it
introduces no self-preference bias and costs nothing.

RAGAS parallelizes its own judge calls by default and, left unconstrained, comfortably exceeds the
NVIDIA endpoint's ~40 requests/minute limit on its own. Every call RAGAS makes goes through the same
ChatOpenAI instance, which is built here with `rate_limiter=eval.judge.SHARED_RATE_LIMITER` -- the
identical rate limiter object eval/judge.py uses for its own two judged tasks -- so the whole eval
run shares one throughput budget. `RAGAS_MAX_WORKERS = 1` on top of that means RAGAS never has more
than one request in flight at a time: the shared limiter already caps the rate a new request can
*start* at, but a strict start-time cap alone still lets two slow in-flight requests overlap when
concurrency is above 1, and it is exactly that overlap that can look like a burst to the endpoint.
One worker removes that possibility outright, at the cost of a slower (but still well within
RAGAS_TIMEOUT_SECONDS) run.

RunConfig also configures RAGAS's own retry-with-backoff (RunConfig.max_retries / max_wait, which
wraps every individual RAGAS judge call in tenacity's `wait_random_exponential`, i.e. exponential
backoff with jitter). `exception_types` is scoped to exactly the transient errors eval/judge.py
itself retries on -- `RETRYABLE_JUDGE_ERRORS`: RateLimitError (429), APIStatusError (covers 5xx),
APIConnectionError, APITimeoutError -- so the retry budget is spent on errors retrying can actually
fix, not burned uselessly against a real bug on every one of RunConfig.max_retries attempts before
failing anyway.

A single row/metric that still exhausts that retry budget must never take the other 20 rows'
worth of already-good scores down with it. `evaluate(..., raise_exceptions=False)` is what makes
that true: ragas.executor.Executor catches the exception per job (one job = one (row, metric)
pair) and reports `np.nan` for that job alone, logging `"Exception raised in Job[N]: ..."` through
the stdlib `logging` module rather than raising. `_RagasJobFailureCollector` below is a temporary
logging.Handler attached to that logger for the duration of the call, which turns each of those
log records back into an explicit (row_index, metric_name, error) triple using the same
`len(metrics) * row + metric_index` job numbering `ragas.evaluate()` uses internally to submit
jobs and reassemble the score dataframe (confirmed by reading the installed `ragas/evaluation.py`
and `ragas/executor.py`: `Executor.submit` assigns each job `counter = len(self.jobs)` at the
moment it is submitted, and `evaluate()` submits jobs row by row, in `RAGAS_METRICS` order within
each row, so the counter is exactly `len(RAGAS_METRICS) * i + j` for row `i` (its position *in the
`EvaluationDataset` passed to this call*) and metric `j`).

That row position is local to this call's own dataset, not the row's position in the full 21-row
golden set. `eval/run.py` only ever passes the rows that survived far enough to reach RAGAS at all
(`scored_records`, which already excludes any row whose `/query` or judge call errored out earlier
in the run), so whenever an earlier golden row is missing from this call, every later row's local
position here is offset from its real golden-set position by however many earlier rows are
missing. Treating the local position as if it were the golden-set position mislabels the failure
(observed in practice: a run where golden rows 12, 13, and 17 (1-indexed) errored before reaching
RAGAS reported a context_precision failure as row 15 when the row that actually failed -- visible
from the error text and the `question` field recorded alongside it -- was row 18; 18 minus the 3
earlier missing rows is exactly 15). The fix: every row dict passed to `run_ragas_metrics` below
must carry its own real golden-set index under `"golden_index"`, and `run_ragas_metrics` reports
`row_index` in `row_failures` (and in every row-labelled log line) from that field, never from the
local position -- while still using the local position for the internal (row, metric) lookup
against `enumerate(records)`, which iterates in this call's own local order. `run_ragas_metrics`
then reports each such failure by name -- exactly which row and which metric could not be scored,
and why -- instead of collapsing it into an anonymous NaN.

That failure case is different from a metric being legitimately undefined for a given row. RAGAS's
own faithfulness implementation (`Faithfulness._ascore`) returns `np.nan` on purpose when the answer
contains zero extractable factual statements -- exactly what a pure advice-refusal answer looks
like, since there is nothing to check faithfulness of. That is not a dropped row or a silent
failure; it is a real, deterministic property of that row's answer, so it is reported as an
undefined value for that one metric on that one row (see `run_ragas_metrics` below), never
coerced to 0 or 1. `run_ragas_metrics` tells the two cases apart using the failure collector: a NaN
with a matching (row, metric) entry in the collector is a real failure and is reported as one; a NaN
with no matching entry is RAGAS's own legitimate "nothing to check" result.
"""

from __future__ import annotations

import asyncio
import logging

import textstat
from langchain_core.embeddings import Embeddings
from langchain_openai import ChatOpenAI
from ragas import EvaluationDataset, SingleTurnSample, evaluate
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import answer_relevancy, context_precision, faithfulness
from ragas.run_config import RunConfig

from app.config import Settings
from app.providers.embeddings import OllamaEmbedder
from eval.judge import RETRYABLE_JUDGE_ERRORS, SHARED_RATE_LIMITER, validate_judge_settings

# The metrics RAGAS scores, and the exact order they are submitted to evaluate() in. Job numbering
# inside ragas.evaluate() is `len(RAGAS_METRICS) * local_row_index + metric_index_in_this_list` (see
# ragas/evaluation.py: `executor.submit(metric.single_turn_ascore, sample, ..., name=f"{metric.name}
# -{i}")` submitted in this list's order for every row), so this list is also how
# _RagasJobFailureCollector's job numbers get decoded back into (local_row_index, metric) pairs
# below. `local_row_index` is this call's own row position, not the golden-set row index -- see the
# module docstring above for why those two differ and how run_ragas_metrics translates between them.
RAGAS_METRICS = [faithfulness, answer_relevancy, context_precision]

# RAGAS concurrency. The outbound rate is already capped by SHARED_RATE_LIMITER regardless of this
# number; max_workers=1 additionally guarantees at most one request in flight at a time, so a slow
# response can never overlap with the next request the limiter allows to start (see module
# docstring for why that overlap -- not just the start-time rate -- is the thing that risked 429s).
RAGAS_MAX_WORKERS = 1

# 21 rows x up to 3 metrics each, and context_precision alone makes one sequential judge call per
# retrieved context (up to 5 per row), all sharing one 35-requests/minute limiter -- the total call
# volume for a full run is in the hundreds. RAGAS's own per-row timeout (RunConfig.timeout) has to
# be generous enough that a row waiting its turn behind other rows' calls on the same shared
# limiter is not itself mistaken for a hang and cancelled mid-request.
RAGAS_TIMEOUT_SECONDS = 600

# A generous but bounded retry budget for the transient errors in RETRYABLE_JUDGE_ERRORS. max_wait
# is the cap tenacity's wait_random_exponential backs off to per attempt; max_retries is the number
# of attempts before RAGAS gives up on that one (row, metric) job and reports it as a named failure
# rather than crashing the batch. Kept at or above eval.judge._MAX_RETRIES (12, configurable via
# JUDGE_MAX_RETRIES) so RAGAS's own retry budget for a (row, metric) job is never the tighter of the
# two -- a job going through RAGAS should get at least as many attempts as a direct judge call does.
RAGAS_MAX_RETRIES = 12
RAGAS_MAX_WAIT_SECONDS = 90


class _RagasJobFailureCollector(logging.Handler):
    """Captures ragas.executor's "Exception raised in Job[N]: ExcName(message)" log records.

    Attach for the duration of one evaluate() call (see run_ragas_metrics), then decode the
    collected job numbers back into (local_row_index, metric_name) pairs -- `local_row_index` is
    this call's own row position, which run_ragas_metrics then translates to the row's real
    golden-set index before reporting it (see the module docstring for why those two indices
    differ). This is what lets a retry-exhausted failure be reported by name instead of
    disappearing into an indistinguishable NaN alongside RAGAS's own legitimate "nothing to score"
    NaNs.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.failures: list[tuple[int, str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        # ragas/executor.py's Executor.wrap_callable_with_index logs exactly:
        #   logger.error("Exception raised in Job[%s]: %s(%s)", counter, exec_name, exec_message)
        # Read the un-formatted args rather than parsing record.getMessage(), so this never breaks
        # on a message containing "(" or ")" of its own.
        if record.name == "ragas.executor" and record.args and len(record.args) == 3:
            counter, exec_name, exec_message = record.args
            self.failures.append((int(counter), str(exec_name), str(exec_message)))


class _RagasOllamaEmbeddings(Embeddings):
    """Adapts app.providers.embeddings.OllamaEmbedder (async) to the langchain Embeddings interface
    RAGAS's LangchainEmbeddingsWrapper expects (sync embed_query/embed_documents plus async
    aembed_query/aembed_documents). This is the embedding model used for answer_relevancy only; it
    is never the generator and never the judge, so it carries no self-preference risk.
    """

    def __init__(self, embedder: OllamaEmbedder):
        self._embedder = embedder

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return asyncio.run(self._embedder.embed(texts))

    def embed_query(self, text: str) -> list[float]:
        return asyncio.run(self._embedder.embed([text]))[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embedder.embed(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return (await self._embedder.embed([text]))[0]


def build_ragas_llm(settings: Settings) -> LangchainLLMWrapper:
    """The judge LLM RAGAS uses for faithfulness/answer_relevancy/context_precision.

    Same NVIDIA endpoint, same temperature=0 and thinking-disabled settings, and the same shared
    rate limiter as eval/judge.py's two judged tasks (see module docstring).

    `model_kwargs={"response_format": {"type": "json_object"}}` is added on top of that: every one
    of RAGAS's own prompts (faithfulness's statement/NLI prompts, answer_relevancy's question
    generation, context_precision's verification) already asks the model for JSON matching a
    Pydantic schema, parsed by RAGAS's own RagasOutputParser. That parser's "fix a malformed
    response" retry path (ragas.prompt.pydantic_prompt.RagasOutputParser.parse_output_string) has
    no exception handling around its own retry attempt, so a single malformed JSON response used to
    crash the entire run with an unrelated pydantic ValidationError instead of a clean retry.
    Constraining the model to valid JSON syntax at the API level, the same way eval/judge.py already
    does for its own two judged tasks, avoids ever entering that fragile path.
    """
    validate_judge_settings(settings)
    chat = ChatOpenAI(
        base_url=settings.JUDGE_BASE_URL,
        api_key=settings.JUDGE_API_KEY,
        model=settings.JUDGE_MODEL,
        temperature=0,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        model_kwargs={"response_format": {"type": "json_object"}},
        rate_limiter=SHARED_RATE_LIMITER,
    )
    return LangchainLLMWrapper(chat)


def build_ragas_embeddings(settings: Settings) -> LangchainEmbeddingsWrapper:
    embedder = OllamaEmbedder(base_url=settings.OLLAMA_BASE_URL, model=settings.EMBED_MODEL)
    return LangchainEmbeddingsWrapper(_RagasOllamaEmbeddings(embedder))


def run_ragas_metrics(rows: list[dict], *, settings: Settings) -> tuple[list[dict], list[dict]]:
    """Score faithfulness, answer_relevancy, and context_precision for every row in one RAGAS run.

    `rows` is a list of dicts with keys "question", "answer", "contexts" (list[str], the full
    retrieved chunk text, not snippets), "ground_truth_answer", and "golden_index" (the row's real
    0-based position in the full golden set -- see the module docstring for why this must be the
    row's *golden-set* position and not simply its position in this call's own `rows` list: the
    caller only ever passes rows that survived far enough to reach RAGAS, so those two positions
    diverge whenever an earlier golden row errored out before this call). Returns
    `(records, row_failures)`:

    `records` has one dict per input row, in the same order, with keys "faithfulness",
    "answer_relevancy", "context_precision" -- never fewer than `len(rows)`, since RAGAS dropping a
    row's result entirely (as opposed to scoring it np.nan) still raises (see below). A metric value
    of `None` means either RAGAS legitimately found nothing to score, or that (row, metric) is also
    named in `row_failures` because it genuinely failed after exhausting its retry budget -- see the
    module docstring for how the two are told apart.

    `row_failures` is a list of dicts, one per (row, metric) pair that failed for a real reason (a
    retryable error that did not resolve inside RAGAS_MAX_RETRIES attempts, or a per-job timeout),
    each with keys "row_index", "question", "metric", "error". "row_index" is the row's real
    golden-set index (taken from that row's own "golden_index"), never this call's local row
    position, and is `None` if the row dict has no "golden_index" -- a missing index is reported as
    unknown rather than guessed at, since a confidently wrong row number is worse than none. A 429
    or other transient failure on one row/metric never aborts the batch (see module docstring);
    this list is how that failure is still surfaced by name instead of silently vanishing into an
    indistinguishable NaN.
    """
    samples = [
        SingleTurnSample(
            user_input=row["question"],
            response=row["answer"],
            retrieved_contexts=row["contexts"] or [""],
            reference=row["ground_truth_answer"],
        )
        for row in rows
    ]
    dataset = EvaluationDataset(samples=samples)
    run_config = RunConfig(
        max_workers=RAGAS_MAX_WORKERS,
        max_retries=RAGAS_MAX_RETRIES,
        max_wait=RAGAS_MAX_WAIT_SECONDS,
        timeout=RAGAS_TIMEOUT_SECONDS,
        exception_types=RETRYABLE_JUDGE_ERRORS,
    )

    failure_collector = _RagasJobFailureCollector()
    ragas_executor_logger = logging.getLogger("ragas.executor")
    ragas_executor_logger.addHandler(failure_collector)
    try:
        result = evaluate(
            dataset=dataset,
            metrics=RAGAS_METRICS,
            llm=build_ragas_llm(settings),
            embeddings=build_ragas_embeddings(settings),
            run_config=run_config,
            # False, not True: a single (row, metric) job that exhausts its retry budget must not
            # take every other row's already-good score down with it (see module docstring). The
            # failure is not silently lost either -- _RagasJobFailureCollector, attached above,
            # records exactly which job failed and why, decoded back into (row, metric) below.
            raise_exceptions=False,
            show_progress=False,
        )
    finally:
        ragas_executor_logger.removeHandler(failure_collector)

    num_metrics = len(RAGAS_METRICS)
    # Keyed on local_row_index (this call's own row position), because it is cross-checked below
    # against `enumerate(records)`, which iterates in that same local order. This is deliberately
    # NOT the golden-set index -- see the module docstring and run_ragas_metrics' own docstring.
    row_metric_failures: dict[tuple[int, str], str] = {}
    row_failures: list[dict] = []
    for counter, exec_name, exec_message in failure_collector.failures:
        local_row_index = counter // num_metrics
        metric_name = RAGAS_METRICS[counter % num_metrics].name
        error = f"{exec_name}: {exec_message}"
        row_metric_failures[(local_row_index, metric_name)] = error
        row = rows[local_row_index] if local_row_index < len(rows) else None
        row_failures.append(
            {
                # The row's real position in the full golden set, taken from that row's own
                # "golden_index" -- never `local_row_index`, which is only this call's position
                # and is wrong whenever an earlier golden row is missing from `rows` (see the
                # module docstring for the mislabeling this fixes).
                "row_index": row.get("golden_index") if row is not None else None,
                "question": row["question"] if row is not None else None,
                "metric": metric_name,
                "error": error,
            }
        )

    records = result.to_pandas().to_dict(orient="records")
    if len(records) != len(rows):
        raise RuntimeError(
            f"RAGAS returned {len(records)} scored rows for {len(rows)} input rows. A row was "
            "dropped instead of failing loudly; this must never happen silently."
        )
    for i, record in enumerate(records):
        for metric_name in ("faithfulness", "answer_relevancy", "context_precision"):
            if metric_name not in record:
                raise RuntimeError(
                    f"Row {i} ({rows[i]['question']!r}) has no {metric_name} column at all in "
                    "RAGAS's output. That is a dropped row, not a legitimate NaN, and must never "
                    "happen silently."
                )
            value = record[metric_name]
            if isinstance(value, float) and value != value:  # NaN check, no numpy/math import
                # Console labels below use the row's real golden-set index (its "golden_index"),
                # never the local loop index `i`, for the same reason row_failures does above: `i`
                # is only this call's own row position and prints a wrong row number whenever an
                # earlier golden row is missing from `rows`.
                golden_index = rows[i].get("golden_index")
                row_label = (
                    f"row {golden_index}" if golden_index is not None else f"row (local index {i})"
                )
                failure_reason = row_metric_failures.get((i, metric_name))
                if failure_reason is not None:
                    print(
                        f"RAGAS FAILURE (not a legitimate NaN): {row_label} "
                        f"({rows[i]['question']!r}) "
                        f"{metric_name} could not be scored -- it exhausted its retry budget: "
                        f"{failure_reason}. Reported as null and excluded from this metric's mean "
                        "for this row; every other row and metric in this run is unaffected."
                    )
                else:
                    print(
                        f"NOTE: {row_label} ({rows[i]['question']!r}) has an undefined "
                        f"{metric_name} -- RAGAS found nothing to score for this row on this "
                        "metric (see the run_ragas_metrics docstring). Reported as null, "
                        "excluded from that metric's mean for this row, not coerced to 0 or 1."
                    )
                record[metric_name] = None
    return records, row_failures


def reading_grade_level(text: str) -> float:
    """Flesch-Kincaid grade level. Reported only, never gated (see eval/README.md)."""
    return textstat.flesch_kincaid_grade(text)
