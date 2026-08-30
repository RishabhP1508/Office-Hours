"""Eval CLI: scores the running orchestrator against eval/golden.jsonl and exits non-zero on FAIL.

Run against the published port from the host:
    python -m eval.run

Run inside the compose network, against the container's own hostname (this is how the Phase 1
verification commands run it):
    docker compose exec -T orchestrator python -m eval.run

ORCHESTRATOR_URL overrides the target and defaults to http://localhost:8000.

This module never authors, edits, or reorders eval/golden.jsonl, never loosens the thresholds in
THRESHOLDS below, and never substitutes a different judge on failure -- see eval/judge.py and
eval/README.md for why. A run that fails a gated metric is reporting a real number, not a bug in
this script.

A single row failing -- the /query call 502ing, or a judge call failing -- must never abort the
whole run. Every row's /query call and judge calls are wrapped so a failure is recorded against
that row (with the real error: exception class, HTTP status if any, and the full response body,
which is where the orchestrator's actual failure reason lives) and the loop moves on. The run
always reaches the point of writing eval/results/<timestamp>.json. Metrics are computed only over
rows that scored successfully, and every reported number carries both the count actually used and
the total row count it was drawn from, so a partial subset is visible rather than silently averaged
over fewer rows than it looks like. If any row errored, the run is INCOMPLETE and the overall
verdict is FAIL regardless of the metric values -- this is not a threshold and is not negotiable.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.config import get_settings
from eval.judge import (
    EmptyAnswerError,
    classify_refusal,
    get_judge_client,
    judge_selfcheck,
    score_comprehensibility,
    validate_judge_settings,
)
from eval.metrics import reading_grade_level, run_ragas_metrics

GOLDEN_PATH = Path(__file__).parent / "golden.jsonl"
RESULTS_DIR = Path(__file__).parent / "results"
EXPECTED_ROW_COUNT = 21
REQUIRED_FIELDS = (
    "question",
    "ground_truth_answer",
    "source_urls",
    "is_advice",
    "verified_on",
    "volatility",
    "multi_part",
)

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8000")

# --- Thresholds: fixed and external. Copied verbatim from the Phase 1 spec. Never change one of
# --- these to make a run pass; a failing number here is information about the system, not a bug in
# --- this file. GATED metrics decide the exit code; REPORTED metrics are printed but never gate.
THRESHOLDS = {
    "faithfulness": {"op": ">=", "value": 0.85, "gated": True},
    "answer_relevancy": {"op": ">=", "value": 0.75, "gated": True},
    "context_precision": {"op": ">=", "value": 0.70, "gated": True},
    "false_refusal_rate": {"op": "<=", "value": 0.10, "gated": True},
    "advice_leakage_rate": {"op": "<=", "value": 0.10, "gated": True},
    "comprehensibility": {"op": ">=", "value": 3.5, "gated": True},
    "citation_hallucination_rate": {"op": "==", "value": 0.0, "gated": True},
    "unreferenced_citation_rate": {"op": None, "value": None, "gated": False},
    "reading_grade_level": {"op": None, "value": None, "gated": False},
}

_BRACKET_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def load_golden_set() -> list[dict]:
    """Load and validate eval/golden.jsonl. Fails loudly rather than silently trimming the set.

    The file has no trailing newline by design (see the Phase 1 brief); iterating a file's lines
    in Python yields the final line regardless, so that is not special-cased here.
    """
    rows = []
    with GOLDEN_PATH.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{GOLDEN_PATH}:{lineno}: invalid JSON: {exc}") from exc
            missing = [name for name in REQUIRED_FIELDS if name not in row]
            if missing:
                raise RuntimeError(
                    f"{GOLDEN_PATH}:{lineno}: row is missing required field(s) {missing}: {row}"
                )
            rows.append(row)
    if len(rows) != EXPECTED_ROW_COUNT:
        raise RuntimeError(
            f"Expected exactly {EXPECTED_ROW_COUNT} golden rows, found {len(rows)} in "
            f"{GOLDEN_PATH}. eval/golden.jsonl is hand-authored by the user and read-only to this "
            "tool -- fix the file by hand, never from this script."
        )
    return rows


_QUERY_TIMEOUT_SECONDS = 600.0  # the local dev generator can take several minutes per question


def query_orchestrator(client: httpx.Client, question: str) -> dict:
    response = client.post(
        f"{ORCHESTRATOR_URL}/query", json={"question": question}, timeout=_QUERY_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return response.json()


def build_error_record(exc: Exception, elapsed_seconds: float) -> dict:
    """Capture everything needed to diagnose a row failure, instead of letting it propagate and
    erasing itself. For an httpx.HTTPStatusError this is the whole point of the fix: the response
    body holds the orchestrator's real `detail` string (see app/main.py), and
    response.raise_for_status() does not destroy that body -- it is only lost if nothing reads it
    before the process dies.
    """
    status_code = None
    body = None
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        try:
            body = exc.response.text
        except Exception:  # pragma: no cover - defensive only
            body = None
    else:
        # Covers exceptions from the judge client (e.g. openai's APIStatusError) that also carry
        # a `.response`, without hardcoding a dependency on the openai package here.
        response = getattr(exc, "response", None)
        if response is not None:
            status_code = getattr(response, "status_code", None)
            try:
                body = response.text
            except Exception:  # pragma: no cover - defensive only
                body = None
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "status_code": status_code,
        "body": body,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def parse_cited_indices(answer_text: str) -> set[int]:
    """Every bracketed numeric reference in the answer, including comma-separated groups like
    "[1, 3]". Matches prompts.py::format_context's [1]..[k] numbering.
    """
    indices: set[int] = set()
    for match in _BRACKET_RE.finditer(answer_text):
        for part in match.group(1).split(","):
            indices.add(int(part.strip()))
    return indices


def analyze_citations(answer_text: str, num_contexts: int, num_citations: int) -> dict:
    """Citation hallucination (gated at zero) and unreferenced citations (reported only).

    Hallucination: a bracketed index the model used that is outside 1..num_contexts, i.e. it cites
    something that was never retrieved for this query.
    Unreferenced: a returned citation (there are `num_citations` of them, one per retrieved chunk in
    Phase 0) whose 1-based position never appears as a bracketed reference anywhere in the answer.
    Phase 4's citation verifier is what changes Phase 0's "return every retrieved chunk as a
    citation" behavior, so this is reported, not gated, here.
    """
    cited = parse_cited_indices(answer_text)
    valid_range = set(range(1, num_contexts + 1))
    hallucinated = cited - valid_range
    referenced_positions = cited & set(range(1, num_citations + 1))
    unreferenced_count = num_citations - len(referenced_positions)
    return {
        "cited_indices": sorted(cited),
        "hallucinated_indices": sorted(hallucinated),
        "hallucinated_count": len(hallucinated),
        "unreferenced_count": max(unreferenced_count, 0),
    }


def mean_with_n(values: list[float | None]) -> tuple[float | None, int]:
    """Mean over the non-None values, plus how many values actually went into it."""
    present = [v for v in values if v is not None]
    if not present:
        return None, 0
    return statistics.mean(present), len(present)


def metric_stat(values: list[float | None], total: int) -> dict:
    """A metric's aggregate value together with BOTH the denominator actually used (n: how many
    scored rows contributed a real, non-null value) and the total row count the subset is drawn
    from (total: including rows that errored or were never scored). Printing only the mean would
    hide a subset that lost rows to an error; this makes it visible as "n=4/6" instead.
    """
    value, n = mean_with_n(values)
    return {"value": value, "n": n, "total": total}


def rate_stat(numerator: int, n: int, total: int) -> dict:
    """Same (value, n, total) shape as metric_stat, for rates computed as numerator/n rather than
    a mean. `value` is None (not 0.0) when n is 0, so a category with zero scored rows never
    silently reports a perfect-looking rate.
    """
    value = (numerator / n) if n else None
    return {"value": value, "n": n, "total": total}


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _fmt_cell(stat: dict) -> str:
    return f"{_fmt(stat['value'])}({stat['n']}/{stat['total']})"


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile (the common definition, e.g. numpy's default). Avoids
    statistics.quantiles, which requires at least two data points.
    """
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] * (c - k) + ordered[c] * (k - f)


def subset_summary(row_records: list[dict], key: str, value) -> dict:
    """Aggregate metrics for the rows where golden[key] == value, computed only over rows that
    scored successfully (not errored). `total` is how many golden rows fall in this category at
    all (errored or not); `n`, at the top level and per metric, is how many of those actually
    contributed a score. A category that lost rows to an error shows up as n < total, never as a
    silently smaller-sample average that looks the same as a complete one.
    """
    subset_all = [r for r in row_records if r["golden"][key] == value]
    subset_scored = [r for r in subset_all if not r["errored"]]
    total = len(subset_all)
    return {
        "n": len(subset_scored),
        "total": total,
        "faithfulness": metric_stat([r.get("faithfulness") for r in subset_scored], total),
        "answer_relevancy": metric_stat([r.get("answer_relevancy") for r in subset_scored], total),
        "context_precision": metric_stat(
            [r.get("context_precision") for r in subset_scored], total
        ),
        "comprehensibility": metric_stat(
            [r.get("comprehensibility") for r in subset_scored], total
        ),
        "reading_grade_level": metric_stat(
            [r.get("reading_grade_level") for r in subset_scored], total
        ),
    }


def print_subset_table(title: str, rows: list[tuple[str, dict]]) -> None:
    print(f"\n--- Subset: {title} (value shown as value(n_scored/total_in_category)) ---")
    header = (
        f"{'group':<10}{'n/total':>9}  {'faithfulness':>16}  {'ans_relevancy':>16}  "
        f"{'ctx_precision':>16}  {'comprehens.':>14}  {'reading_grade':>16}"
    )
    print(header)
    for label, s in rows:
        n_total = f"{s['n']}/{s['total']}"
        print(
            f"{label:<10}{n_total:>9}  {_fmt_cell(s['faithfulness']):>16}  "
            f"{_fmt_cell(s['answer_relevancy']):>16}  {_fmt_cell(s['context_precision']):>16}  "
            f"{_fmt_cell(s['comprehensibility']):>14}  {_fmt_cell(s['reading_grade_level']):>16}"
        )


def build_aggregate_stats(golden_rows: list[dict], scored_records: list[dict]) -> tuple[dict, dict]:
    """Aggregate stats for every metric in THRESHOLDS, each as {"value", "n", "total"}, computed
    only over `scored_records` (rows that did not error). `total` for the overall run is always
    len(golden_rows) -- the full 21 -- regardless of how many rows actually scored, so an
    incomplete run is visible in the numbers themselves, not just in the errored_rows count.
    Returns (aggregate_stats, citation_detail) where citation_detail holds the raw counts behind
    the two citation rates for the printed detail lines.
    """
    total_rows = len(golden_rows)

    def mstat(field: str) -> dict:
        return metric_stat([r.get(field) for r in scored_records], total_rows)

    total_advice = sum(1 for g in golden_rows if g["is_advice"])
    total_non_advice = total_rows - total_advice
    advice_scored = [r for r in scored_records if r["golden"]["is_advice"]]
    non_advice_scored = [r for r in scored_records if not r["golden"]["is_advice"]]

    false_refusal_stat = rate_stat(
        sum(1 for r in non_advice_scored if r["refusal_classification"] == "REFUSAL"),
        len(non_advice_scored),
        total_non_advice,
    )
    advice_leakage_stat = rate_stat(
        sum(1 for r in advice_scored if r["refusal_classification"] == "ANSWER"),
        len(advice_scored),
        total_advice,
    )

    rows_with_hallucination = sum(1 for r in scored_records if r["hallucinated_count"] > 0)
    citation_hallucination_stat = rate_stat(
        rows_with_hallucination, len(scored_records), total_rows
    )

    total_unreferenced = sum(r["unreferenced_count"] for r in scored_records)
    total_citations_returned = sum(r["num_citations_returned"] for r in scored_records)
    unreferenced_value = (
        total_unreferenced / total_citations_returned if total_citations_returned else None
    )
    unreferenced_stat = {
        "value": unreferenced_value,
        "n": total_citations_returned,
        "total": total_citations_returned,
    }

    aggregate_stats = {
        "faithfulness": mstat("faithfulness"),
        "answer_relevancy": mstat("answer_relevancy"),
        "context_precision": mstat("context_precision"),
        "false_refusal_rate": false_refusal_stat,
        "advice_leakage_rate": advice_leakage_stat,
        "comprehensibility": mstat("comprehensibility"),
        "citation_hallucination_rate": citation_hallucination_stat,
        "unreferenced_citation_rate": unreferenced_stat,
        "reading_grade_level": mstat("reading_grade_level"),
    }
    citation_detail = {
        "rows_with_hallucination": rows_with_hallucination,
        "total_hallucinated": sum(r["hallucinated_count"] for r in scored_records),
        "total_cited": sum(len(r["cited_indices"]) for r in scored_records),
        "total_unreferenced": total_unreferenced,
        "total_citations_returned": total_citations_returned,
    }
    return aggregate_stats, citation_detail


def main() -> int:  # noqa: C901 - the per-row error handling adds branches by nature of the fix
    settings = get_settings()
    validate_judge_settings(settings)

    print("=== Office Hours eval run ===")
    print(f"Orchestrator: {ORCHESTRATOR_URL}")
    print(f"Judge model requested: {settings.JUDGE_MODEL}")

    golden_rows = load_golden_set()
    print(f"Golden set: {len(golden_rows)} rows loaded from {GOLDEN_PATH}")

    judge_client = get_judge_client(settings)

    row_records: list[dict] = []
    served_model: str | None = None
    run_start = time.monotonic()

    with httpx.Client() as http_client:
        for i, golden in enumerate(golden_rows):
            question = golden["question"]
            record: dict = {
                "index": i,
                "golden": golden,
                "question": question,
                "errored": False,
                "error": None,
            }

            query_start = time.monotonic()
            try:
                response = query_orchestrator(http_client, question)
            except Exception as exc:  # noqa: BLE001 - recorded per-row, loop must continue
                elapsed = time.monotonic() - query_start
                record["elapsed_seconds"] = round(elapsed, 3)
                record["errored"] = True
                record["error"] = build_error_record(exc, elapsed)
                row_records.append(record)
                err = record["error"]
                print(
                    f"  [{i + 1}/{len(golden_rows)}] QUERY FAILED after {elapsed:.1f}s: "
                    f"{question[:70]!r} -> {err['type']} (status={err['status_code']}): "
                    f"{err['message'][:200]}"
                )
                continue
            elapsed = time.monotonic() - query_start
            record["elapsed_seconds"] = round(elapsed, 3)

            answer_text = response["answer"]
            num_citations_returned = len(response["citations"])

            # The service must never be trusted to have returned something usable just because
            # the HTTP call succeeded: an empty or whitespace-only answer is unusable output that
            # must never be judged, scored, or averaged in as if it were a real one (see the Phase
            # 1 defect fix notes -- this is what makes app/providers/llm.py's own empty-content
            # check a belt-and-braces defence rather than the only one). Recorded as errored so the
            # "any errored row means the run is INCOMPLETE" guard below fires on it, same as any
            # other unusable row.
            if not answer_text or not answer_text.strip():
                exc = EmptyAnswerError(
                    f"/query returned an empty or whitespace-only answer after {elapsed:.3f}s "
                    f"({num_citations_returned} citation(s) returned). Never judged, never scored: "
                    "an empty answer is not comprehensible and has nothing to check faithfulness "
                    "or relevancy against."
                )
                record["errored"] = True
                record["error"] = build_error_record(exc, elapsed)
                row_records.append(record)
                print(
                    f"  [{i + 1}/{len(golden_rows)}] EMPTY ANSWER after {elapsed:.1f}s: "
                    f"{question[:70]!r} ({num_citations_returned} citation(s) returned, not judged)"
                )
                continue

            contexts = [c["content"] for c in response["contexts"]]
            citation_stats = analyze_citations(
                answer_text, num_contexts=len(contexts), num_citations=num_citations_returned
            )

            judge_start = time.monotonic()
            try:
                comprehensibility, served_model = score_comprehensibility(
                    judge_client, settings.JUDGE_MODEL, question, answer_text
                )
                classification, served_model = classify_refusal(
                    judge_client, settings.JUDGE_MODEL, question, answer_text
                )
            except Exception as exc:  # noqa: BLE001 - recorded per-row, loop must continue
                judge_elapsed = time.monotonic() - judge_start
                record["errored"] = True
                record["error"] = build_error_record(exc, judge_elapsed)
                row_records.append(record)
                err = record["error"]
                print(
                    f"  [{i + 1}/{len(golden_rows)}] JUDGE FAILED after {judge_elapsed:.1f}s "
                    f"(query succeeded in {elapsed:.1f}s): {question[:70]!r} -> {err['type']}: "
                    f"{err['message'][:200]}"
                )
                continue

            record.update(
                {
                    "answer": answer_text,
                    "ground_truth_answer": golden["ground_truth_answer"],
                    "contexts": contexts,
                    "num_citations_returned": num_citations_returned,
                    "comprehensibility": comprehensibility,
                    "refusal_classification": classification,
                    "reading_grade_level": reading_grade_level(answer_text),
                    **citation_stats,
                }
            )
            row_records.append(record)
            print(
                f"  [{i + 1}/{len(golden_rows)}] queried + judged in {elapsed:.1f}s: "
                f"{question[:70]!r}"
            )

    print(f"\nJudge model served (from the API's own response): {served_model}")

    errored_records = [r for r in row_records if r["errored"]]
    scored_records = [r for r in row_records if not r["errored"]]
    errored_rows = len(errored_records)
    empty_answer_records = [r for r in errored_records if r["error"]["type"] == "EmptyAnswerError"]
    empty_answer_rows = len(empty_answer_records)

    # --- Determinism check (DoD 4b): score the same row's comprehensibility twice in this run. ---
    # Picks the first row with an actual, non-empty answer rather than always row 0: row 0 erroring
    # (in particular, on an empty answer -- see EmptyAnswerError above) must not make this check
    # either crash or silently "pass" by re-scoring nothing. Re-scoring an empty answer twice would
    # prove nothing about determinism; it only proves the judge is consistent on blank input.
    first = second = None
    determinism_row = next(
        (r for r in row_records if not r["errored"] and r.get("answer", "").strip()),
        None,
    )
    if determinism_row is not None:
        check_label = f"row {determinism_row['index'] + 1}"
        try:
            first, second = judge_selfcheck(
                judge_client,
                settings.JUDGE_MODEL,
                determinism_row["question"],
                determinism_row["answer"],
            )
        except Exception as exc:  # noqa: BLE001 - reported, must not abort the run
            print(f"\n--- Determinism check (judge_selfcheck on {check_label}) ---")
            print(f"FAILED: {type(exc).__name__}: {exc}")
        else:
            print(f"\n--- Determinism check (judge_selfcheck on {check_label}) ---")
            identical = first == second
            print(f"comprehensibility run 1: {first}   run 2: {second}   identical: {identical}")
            if first != second:
                print(
                    "WARNING: temperature=0 did not produce identical scores on the same input. "
                    "Every downstream judge number in this run should be treated as noise."
                )
    else:
        print("\n--- Determinism check (judge_selfcheck) ---")
        print("SKIPPED: no row produced a non-empty answer to check determinism against.")

    # --- RAGAS metrics: faithfulness, answer_relevancy, context_precision, one shared run. ---
    # Only over rows that scored successfully; a row that never got an answer has nothing for
    # RAGAS to score. The whole batch call is also guarded: an unexpected failure in run_ragas_
    # metrics itself (as opposed to one (row, metric) job exhausting its retry budget, which
    # run_ragas_metrics already isolates and reports in ragas_row_failures without raising) must
    # not crash the script before it writes results either.
    ragas_error: str | None = None
    ragas_row_failures: list[dict] = []
    if scored_records:
        print("\nRunning RAGAS metrics (faithfulness, answer_relevancy, context_precision)...")
        ragas_rows = [
            {
                "question": r["question"],
                "answer": r["answer"],
                "contexts": r["contexts"],
                "ground_truth_answer": r["ground_truth_answer"],
                # The row's real 0-based position in the full golden set, NOT its position in
                # this list: scored_records already excludes any row whose /query or judge call
                # errored out earlier in this run, so a row's position here can be lower than its
                # real golden-set position. run_ragas_metrics reports row_failures using this
                # field so a failure is never labeled with the wrong row (see eval/metrics.py's
                # run_ragas_metrics docstring for the mislabeling this fixes).
                "golden_index": r["index"],
            }
            for r in scored_records
        ]
        try:
            ragas_records, ragas_row_failures = run_ragas_metrics(ragas_rows, settings=settings)
        except Exception as exc:  # noqa: BLE001 - must not abort the run before writing results
            ragas_error = f"{type(exc).__name__}: {exc}"
            print(f"RAGAS metrics FAILED for the whole batch: {ragas_error}")
        else:
            for r, record in zip(scored_records, ragas_records, strict=True):
                r["faithfulness"] = record["faithfulness"]
                r["answer_relevancy"] = record["answer_relevancy"]
                r["context_precision"] = record["context_precision"]
            if ragas_row_failures:
                print(
                    f"\n{len(ragas_row_failures)} RAGAS (row, metric) job(s) exhausted their "
                    "retry budget -- named individually above, reported as null in that row's "
                    "metric rather than failing the batch:"
                )
                for failure in ragas_row_failures:
                    row_index = failure["row_index"]
                    row_label = (
                        f"row {row_index + 1}" if row_index is not None else "row (index unknown)"
                    )
                    print(
                        f"  {row_label} ({failure['question'][:70]!r}) "
                        f"{failure['metric']}: {failure['error'][:200]}"
                    )
    else:
        print("\nNo rows scored successfully; skipping RAGAS metrics entirely.")

    # --- Aggregates (computed over scored_records only; every stat carries n and total) ---
    aggregate_stats, citation_detail = build_aggregate_stats(golden_rows, scored_records)

    verdicts = {}
    overall_pass = True
    for metric_name, spec in THRESHOLDS.items():
        stat = aggregate_stats[metric_name]
        value = stat["value"]
        if not spec["gated"]:
            verdicts[metric_name] = None
            continue
        threshold = spec["value"]
        if spec["op"] == ">=":
            passed = value is not None and value >= threshold
        elif spec["op"] == "<=":
            passed = value is not None and value <= threshold
        elif spec["op"] == "==":
            passed = value is not None and value == threshold
        else:
            raise ValueError(f"Unknown operator for {metric_name}: {spec['op']}")
        verdicts[metric_name] = passed
        overall_pass = overall_pass and passed

    incomplete = errored_rows > 0
    if incomplete:
        overall_pass = False

    # --- Print overall table ---
    print(
        f"\n--- Overall metrics (value(n_scored/total_rows)) --- scored {len(scored_records)}/"
        f"{len(golden_rows)} rows"
    )
    print(
        f"{'metric':<30}{'value':>10}  {'n/total':>9}  {'threshold':>12}  {'gate':>10}  "
        f"{'result':>7}"
    )
    for metric_name, spec in THRESHOLDS.items():
        stat = aggregate_stats[metric_name]
        value = stat["value"]
        n_total = f"{stat['n']}/{stat['total']}"
        threshold_str = "n/a" if spec["value"] is None else f"{spec['op']} {spec['value']}"
        gate_str = "GATED" if spec["gated"] else "REPORTED"
        if not spec["gated"]:
            result_str = "--"
        else:
            result_str = "PASS" if verdicts[metric_name] else "FAIL"
        print(
            f"{metric_name:<30}{_fmt(value):>10}  {n_total:>9}  {threshold_str:>12}  "
            f"{gate_str:>10}  {result_str:>7}"
        )
    print(
        f"\ncitation_hallucination detail: {citation_detail['rows_with_hallucination']}/"
        f"{len(scored_records)} scored rows had a hallucinated citation; "
        f"{citation_detail['total_hallucinated']} hallucinated reference(s) out of "
        f"{citation_detail['total_cited']} bracketed reference(s) total."
    )
    print(
        f"unreferenced_citation detail: {citation_detail['total_unreferenced']}/"
        f"{citation_detail['total_citations_returned']} returned citations were never referenced "
        "in the answer text."
    )
    if ragas_error:
        print(f"\nRAGAS FAILURE: {ragas_error}")

    # --- Subset breakdowns ---
    print_subset_table(
        "volatility",
        [
            ("stable", subset_summary(row_records, "volatility", "stable")),
            ("volatile", subset_summary(row_records, "volatility", "volatile")),
        ],
    )
    print_subset_table(
        "is_advice",
        [
            ("false", subset_summary(row_records, "is_advice", False)),
            ("true", subset_summary(row_records, "is_advice", True)),
        ],
    )
    print_subset_table(
        "multi_part",
        [
            ("false", subset_summary(row_records, "multi_part", False)),
            ("true", subset_summary(row_records, "multi_part", True)),
        ],
    )

    # --- Latency distribution: every row that at least attempted a /query call has a real
    # --- elapsed_seconds, whether it succeeded or errored. Slow rows are visible as numbers here
    # --- instead of as a crash.
    latencies = [r["elapsed_seconds"] for r in row_records]
    run_wall_clock = time.monotonic() - run_start
    latency_stats = {
        "min": min(latencies) if latencies else None,
        "median": statistics.median(latencies) if latencies else None,
        "p95": percentile(latencies, 95),
        "max": max(latencies) if latencies else None,
        "total_wall_clock_seconds": round(run_wall_clock, 3),
    }
    print("\n--- Latency: per-row /query call, seconds (includes errored rows) ---")
    print(
        f"min={_fmt(latency_stats['min'])}  median={_fmt(latency_stats['median'])}  "
        f"p95={_fmt(latency_stats['p95'])}  max={_fmt(latency_stats['max'])}  "
        f"total_wall_clock={_fmt(latency_stats['total_wall_clock_seconds'])}s"
    )

    # --- Errored rows: named explicitly, never just a count ---
    if errored_records:
        print(f"\n--- Errored rows ({errored_rows}/{len(golden_rows)}) ---")
        for r in errored_records:
            err = r["error"]
            print(
                f"  row {r['index'] + 1}: {r['question'][:80]!r}\n"
                f"    {err['type']} status={err['status_code']} elapsed={err['elapsed_seconds']}s\n"
                f"    message: {err['message'][:300]}\n"
                f"    body: {(err['body'] or '')[:500]!r}"
            )

    # empty_answer_rows called out on its own line, separate from the generic errored-rows count
    # above: an empty answer is a distinct, specific failure mode (the service produced nothing to
    # judge) and must be visible as such rather than folded into "errored_rows" undifferentiated
    # from a 502 or a judge outage.
    print(
        f"\nempty_answer_rows: {empty_answer_rows}/{len(golden_rows)} row(s) returned an empty or "
        "whitespace-only answer (not judged, not scored, counted in errored_rows above)."
    )

    if incomplete:
        errored_indices = [r["index"] + 1 for r in errored_records]
        print(
            f"\n*** RUN INCOMPLETE: {errored_rows}/{len(golden_rows)} row(s) errored before "
            f"scoring (rows {errored_indices}). ***"
        )
        print(
            "*** An incomplete eval run can never report PASS, regardless of the metric values "
            "above -- see the errored rows detail for the real cause of each failure. ***"
        )

    print(f"\nOVERALL: {'PASS' if overall_pass else 'FAIL'}")

    # --- Write results file ---
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    run_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results_path = RESULTS_DIR / f"{run_timestamp}.json"
    output = {
        "run_at_utc": run_timestamp,
        "orchestrator_url": ORCHESTRATOR_URL,
        "judge_model_requested": settings.JUDGE_MODEL,
        "judge_model_served": served_model,
        "thresholds": THRESHOLDS,
        "total_rows": len(golden_rows),
        "scored_rows": len(scored_records),
        "errored_rows": errored_rows,
        "empty_answer_rows": empty_answer_rows,
        "incomplete": incomplete,
        "rows": [
            {
                "index": r["index"],
                "question": r["question"],
                "is_advice": r["golden"]["is_advice"],
                "volatility": r["golden"]["volatility"],
                "multi_part": r["golden"]["multi_part"],
                "errored": r["errored"],
                "error": r["error"],
                "elapsed_seconds": r["elapsed_seconds"],
                "answer": r.get("answer"),
                "refusal_classification": r.get("refusal_classification"),
                "faithfulness": r.get("faithfulness"),
                "answer_relevancy": r.get("answer_relevancy"),
                "context_precision": r.get("context_precision"),
                "comprehensibility": r.get("comprehensibility"),
                "reading_grade_level": r.get("reading_grade_level"),
                "num_citations_returned": r.get("num_citations_returned"),
                "cited_indices": r.get("cited_indices"),
                "hallucinated_indices": r.get("hallucinated_indices"),
                "hallucinated_count": r.get("hallucinated_count"),
                "unreferenced_count": r.get("unreferenced_count"),
            }
            for r in row_records
        ],
        "aggregates": aggregate_stats,
        "citation_detail": citation_detail,
        "ragas_error": ragas_error,
        "ragas_row_failures": ragas_row_failures,
        "verdicts": verdicts,
        "subset_breakdowns": {
            "volatility": {
                "stable": subset_summary(row_records, "volatility", "stable"),
                "volatile": subset_summary(row_records, "volatility", "volatile"),
            },
            "is_advice": {
                "false": subset_summary(row_records, "is_advice", False),
                "true": subset_summary(row_records, "is_advice", True),
            },
            "multi_part": {
                "false": subset_summary(row_records, "multi_part", False),
                "true": subset_summary(row_records, "multi_part", True),
            },
        },
        "latency": latency_stats,
        "determinism_check": {
            "row_index": determinism_row["index"] if determinism_row is not None else None,
            "row": determinism_row["question"] if determinism_row is not None else None,
            "score_1": first,
            "score_2": second,
        },
        "overall_pass": overall_pass,
    }
    # NEVER write the judge API key or any secret into the results file.
    with results_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote results to {results_path}")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
