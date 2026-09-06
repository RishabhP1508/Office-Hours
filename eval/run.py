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

--- CI mode (EVAL_MODE=ci env var, or --ci on the command line) ---

CI mode is a SEPARATE, narrower gate from a full run, built for GitHub Actions' `pull_request`
trigger (.github/workflows/eval.yml), which gets no repository secret and no GPU. It runs against
the orchestrator with LLM_PROVIDER=stub and EMBED_PROVIDER=stub (deterministic, network-free
providers -- see app/providers/llm.py::StubLLM and app/providers/embeddings.py::StubEmbedder) over
the small fixture corpus at eval/fixtures/sources, never the real one.

CI mode computes, fully programmatically, with no LLM judge and no RAGAS: citation_hallucination_
rate, unreferenced_citation_rate, errored_rows, empty_answer_rows, every subset breakdown, reading_
grade_level (textstat, local), and (since Phase 4 step 3) false_refusal_rate/advice_leakage_rate
read directly from each response's structured `response_type` field via
classify_refusal_structured, never from prose pattern-matching. It SKIPS comprehensibility (the
judge) and faithfulness/answer_relevancy/context_precision (RAGAS) entirely -- never calling
get_judge_client, validate_judge_settings, or eval.metrics.run_ragas_metrics -- and prints those as
"SKIPPED (CI mode)" rather than a number, with a banner stating plainly that no LLM-judged metric
ran and a green CI result is not a passing quality eval. See docs/adr/0004-ci-baselines-vs-
aspirational-thresholds.md for why this exists as a separate gate rather than a relaxed version of
THRESHOLDS: it checks that the plumbing (retrieval, citation mapping, refusal bookkeeping, error
handling) still behaves correctly, never answer quality, and it is scored against
eval/baselines.json ("no worse than the last accepted CI-mode run"), never against THRESHOLDS --
THRESHOLDS stays the Phase 4 aspirational target that only a full run against the real providers is
measured against.

false_refusal_rate and advice_leakage_rate are GATED in CI mode as of Phase 4 step 3 (in
CI_BASELINE_GATE, alongside citation_hallucination_rate), not merely reported. Through Phase 4 step
2 these were reported only: CI mode had no orchestrator-emitted signal to read, so the rate measured
whether StubLLM's own hardcoded advice-detection patterns agreed with eval/golden.jsonl's is_advice
label -- gating on that would have rewarded tuning the stub to match the label, not measuring
anything real. That constraint is gone: the classification now comes from
app/guardrails/classifier.py's rule layer (Layer 1), the SAME production code path a real request
takes, read through the response's `response_type` field exactly as a real caller would see it.
There is no longer a stub-specific decision to game -- gating this rate now measures the rule
layer's real behavior on the fixture corpus, deterministically, every run.

This does NOT mean CI mode measures the full classifier: Layer 2 (the model escalation) never runs
under LLM_PROVIDER=stub (see app/guardrails/classifier.py::classify_advice), so any golden row whose
advice phrasing only Layer 2 would catch shows up here as a leaked advice row. That is an honest
measurement of the rule layer's coverage on its own, not a defect in this eval, and NOT something to
fix by adding a pattern that matches exactly one golden row -- see ADVICE_PATTERNS' own comment in
app/guardrails/classifier.py for why that recreates a tautology this project already removed once.
The refusal-classification bookkeeping (CI_REFUSAL_BOOKKEEPING_GATE) is unchanged and still gated
alongside the rates: that every row in the 15-row non-advice subset and the 6-row advice subset
actually gets scored and classified, independent of what the rate itself says.
"""

from __future__ import annotations

import argparse
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
from eval.judge import EmptyAnswerError
from eval.metrics import reading_grade_level

# Only EmptyAnswerError is imported from eval.judge at module scope: it is a plain exception class
# with no import-time cost, and both modes need it (an empty/whitespace-only answer is recorded as
# an errored row in CI mode too -- see the per-row loop below). eval.judge itself imports only
# `openai` at module level (small, always-installed -- see services/orchestrator/pyproject.toml's
# `eval-ci` extra) and builds its shared rate limiter lazily (eval.judge.get_shared_rate_limiter()),
# so merely importing eval.judge is safe in CI mode too -- but the rest of eval.judge's names
# (validate_judge_settings, get_judge_client, score_comprehensibility, classify_refusal,
# judge_selfcheck) are still imported lazily below, only on the full-mode path, as defence in depth:
# CI mode must never even attempt to construct a judge client against the empty JUDGE_API_KEY a
# `pull_request` job legitimately has. `eval.metrics.run_ragas_metrics` is imported lazily where it
# is used, further down, for the reason explained in eval/metrics.py's own module docstring:
# importing it must never require ragas to be installed.

GOLDEN_PATH = Path(__file__).parent / "golden.jsonl"
RESULTS_DIR = Path(__file__).parent / "results"
BASELINES_PATH = Path(__file__).parent / "baselines.json"
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

# Metrics CI mode never computes, because computing them needs the hosted judge (comprehensibility)
# or RAGAS (faithfulness, answer_relevancy, context_precision) -- see the module docstring's CI mode
# section. Printed as "SKIPPED (CI mode)" and excluded from CI's gating and from the baseline
# comparison entirely; never coerced to a value.
CI_SKIPPED_METRICS = ("faithfulness", "answer_relevancy", "context_precision", "comprehensibility")

# Maps each of the five app/schemas.py::ResponseType members to the same two-value vocabulary
# eval/judge.py::classify_refusal uses ("REFUSAL" or "ANSWER"), so false_refusal_rate and
# advice_leakage_rate stay comparable across CI mode (this mapping) and full mode (the judge).
#
# Why the four non-ANSWER types all collapse to REFUSAL: false_refusal_rate asks "did the system
# fail to produce an answer for an answerable question", and all four of CLARIFY, NO_ANSWER,
# REFUSAL_ADVICE, and BLOCKED_UNVERIFIED are failures to answer, by that definition, regardless of
# which guardrail produced them. This is also what the judge has always effectively done:
# eval/judge.py::REFUSAL_RUBRIC classifies a response that says the sources do not cover the
# question as a REFUSAL, not a third category.
#
# What this collapse COSTS, and why response_type is recorded per row (see the per-row loop below)
# rather than only this collapsed value: collapsing merges "refused an advice-seeking question" with
# "did not know the answer", which ARCHITECTURE.md deliberately treats as two separate failure
# modes, each with its own test. It means advice_leakage_rate can improve because the system
# genuinely did not know (NO_ANSWER) rather than because it recognized the question as advice-
# seeking and refused on purpose (REFUSAL_ADVICE) -- two very different outcomes that read
# identically in this collapsed rate. The collapsed rate is the metric comparable across runs and
# phases; the raw response_type on each row is what to read when the question is which of the two
# actually happened for a given row.
_RESPONSE_TYPE_TO_REFUSAL = {
    "answer": "ANSWER",
    "refusal_advice": "REFUSAL",
    "clarify": "REFUSAL",
    "no_answer": "REFUSAL",
    "blocked_unverified": "REFUSAL",
}


class UnknownResponseTypeError(ValueError):
    """Raised when a /query response has no `response_type` field, or a value outside the five
    app/schemas.py::ResponseType members. Never defaulted to ANSWER and never silently skipped: a
    missing or invalid flag means the orchestrator being measured predates Phase 4's structured
    response type (or is returning something malformed), and assuming a value in either case would
    silently mis-measure the whole run rather than surface the real problem.
    """


def classify_refusal_structured(response_type: str | None) -> str:
    """Map a /query response's `response_type` to "REFUSAL" or "ANSWER" -- see
    _RESPONSE_TYPE_TO_REFUSAL's comment for the mapping and what collapsing it costs. Used in BOTH
    modes: it is the only classification CI mode has (no judge is available there), and it also
    feeds full mode's REPORTED-only *_structured metrics (see compute_structured_refusal_rates)
    alongside the judge's own eval.judge.classify_refusal.
    """
    if response_type not in _RESPONSE_TYPE_TO_REFUSAL:
        raise UnknownResponseTypeError(
            f"/query response has response_type={response_type!r}, which is not one of "
            f"{sorted(_RESPONSE_TYPE_TO_REFUSAL)}. This eval never defaults an unrecognized or "
            "missing response_type to ANSWER, and never skips the row silently -- see "
            "UnknownResponseTypeError's own docstring for why."
        )
    return _RESPONSE_TYPE_TO_REFUSAL[response_type]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Office Hours eval runner. Defaults to a full run against the real providers, judged "
            "and scored by RAGAS. --ci (or EVAL_MODE=ci) runs the CI invariant gate instead -- see "
            "the module docstring."
        )
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Run the CI invariant gate: no judge, no RAGAS, gated against eval/baselines.json "
        "instead of THRESHOLDS. Equivalent to setting EVAL_MODE=ci.",
    )
    return parser.parse_args(argv)


def resolve_ci_mode(argv: list[str]) -> bool:
    args = parse_args(argv)
    return args.ci or os.environ.get("EVAL_MODE", "").strip().lower() == "ci"


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

# --- Baselines: separate from THRESHOLDS, and this is what CI mode actually gates on. ---
# THRESHOLDS above are the fixed, Phase 4 aspirational target; a full local run always reports
# against them and they never move. CI mode runs stub providers over a small fixture corpus, whose
# numbers have no relationship to THRESHOLDS at all (there is no judge, no RAGAS, and the corpus is
# not the real one), so gating CI against THRESHOLDS would either always fail (comparing an
# unrelated stub-provider number against a target set for the real system) or require inventing a
# second, weaker set of thresholds -- which is exactly the "loosen it until it passes" anti-pattern
# this project forbids. Instead, CI compares the CURRENT CI-mode run against the last CI-mode run a
# human accepted (eval/baselines.json's "ci_baseline"), with a small tolerance for float noise, and
# fails if any gated metric got WORSE. See docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md.
#
# Direction and tolerance for each metric CI mode can gate. `higher_is_better=False` covers both
# THRESHOLDS' "<=" metrics and its one "==" metric (citation_hallucination_rate: exactly 0.0 is the
# target, so "no worse" means "no higher"). errored_rows and empty_answer_rows have no THRESHOLDS
# entry at all -- they are structural invariants, not scored metrics -- but a baseline should still
# catch a regression in them, so they are gated the same way with a zero tolerance (this pipeline is
# fully deterministic under stub providers, so two runs over the same fixture corpus and golden set
# should reproduce the same counts exactly).
#
# false_refusal_rate and advice_leakage_rate ARE here as of Phase 4 step 3 (tolerance=0: this
# pipeline is fully deterministic under stub providers, so a regression is never float noise).
# Through Phase 4 step 2 these were deliberately excluded: CI mode had no orchestrator-emitted
# classification, so the rate measured StubLLM's own hardcoded pattern list agreeing with
# eval/golden.jsonl's is_advice label, and gating on that would have rewarded tuning the stub's
# patterns to match the label rather than measuring anything real. That constraint is gone: the
# classification now comes from classify_refusal_structured, reading the SAME response_type field
# app/guardrails/classifier.py's rule layer (real production code, not a stub-only heuristic)
# produces for a real request. There is no longer a stub-specific decision being gated -- see the
# module docstring for what CI mode's rule-layer-only coverage does and does not measure.
CI_BASELINE_GATE = {
    "errored_rows": {"higher_is_better": False, "tolerance": 0},
    "empty_answer_rows": {"higher_is_better": False, "tolerance": 0},
    "citation_hallucination_rate": {"higher_is_better": False, "tolerance": 1e-9},
    "false_refusal_rate": {"higher_is_better": False, "tolerance": 0},
    "advice_leakage_rate": {"higher_is_better": False, "tolerance": 0},
}

# Refusal-classification bookkeeping CI mode ALSO gates, alongside the rate values above (not
# instead of them, now that the rates themselves are gated too): not "is the classification
# correct" but "did every row in each subset get scored and classified at all".
# non_advice_scored_count/advice_scored_count are the false_refusal_rate/advice_leakage_rate stats'
# own "n" (how many of the 15 non-advice / 6 advice golden rows actually got scored, i.e. did not
# error out before reaching classification) -- "higher_is_better" because a regression here means a
# row silently dropped out of the subset. unclassified_rows counts scored rows whose
# refusal_classification is not exactly "REFUSAL" or "ANSWER", which should always be zero since
# classify_refusal_structured has no other return path (it raises instead -- see
# UnknownResponseTypeError); gating it catches a future edit that adds a silent third value.
CI_REFUSAL_BOOKKEEPING_GATE = {
    "non_advice_scored_count": {"higher_is_better": True, "tolerance": 0},
    "advice_scored_count": {"higher_is_better": True, "tolerance": 0},
    "unclassified_rows": {"higher_is_better": False, "tolerance": 0},
}

# Computed and compared against the baseline for tracking, exactly like THRESHOLDS' REPORTED rows,
# but never gates the run either way.
CI_BASELINE_REPORTED = ("unreferenced_citation_rate", "reading_grade_level")


def load_baselines() -> dict:
    if not BASELINES_PATH.exists():
        raise RuntimeError(
            f"{BASELINES_PATH} does not exist. CI mode gates against a recorded baseline, not "
            "THRESHOLDS -- record one from an actual CI-mode run before running CI mode for real "
            "(see docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md)."
        )
    with BASELINES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def compare_to_baseline(
    value: float | int | None,
    baseline_value: float | int | None,
    *,
    higher_is_better: bool,
    tolerance: float,
) -> bool | None:
    """True if `value` is no worse than `baseline_value`, within `tolerance`; None (never coerced to
    a pass or a fail) if either side is missing.
    """
    if value is None or baseline_value is None:
        return None
    if higher_is_better:
        return value >= baseline_value - tolerance
    return value <= baseline_value + tolerance


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


def compute_structured_refusal_rates(golden_rows: list[dict], scored_records: list[dict]) -> dict:
    """FULL MODE ONLY: false_refusal_rate_structured and advice_leakage_rate_structured, computed
    exactly like build_aggregate_stats' false_refusal_rate/advice_leakage_rate above but from each
    row's `refusal_classification_structured` (classify_refusal_structured's mapping of the
    pipeline's own response_type) instead of the judge's `refusal_classification`.

    Both are REPORTED, never gated, in full mode (CI mode already gates the structured version
    directly -- see CI_BASELINE_GATE -- so computing a second copy of the same number there would
    be redundant, which is why this is never called when ci_mode is True). The point of keeping
    both instruments visible side by side is that where the judge and the pipeline disagree, the
    disagreement is itself information about the judge, the guardrail, or both -- not noise to
    average away.
    """
    total_advice = sum(1 for g in golden_rows if g["is_advice"])
    total_non_advice = len(golden_rows) - total_advice
    advice_scored = [r for r in scored_records if r["golden"]["is_advice"]]
    non_advice_scored = [r for r in scored_records if not r["golden"]["is_advice"]]

    false_refusal_structured = rate_stat(
        sum(1 for r in non_advice_scored if r["refusal_classification_structured"] == "REFUSAL"),
        len(non_advice_scored),
        total_non_advice,
    )
    advice_leakage_structured = rate_stat(
        sum(1 for r in advice_scored if r["refusal_classification_structured"] == "ANSWER"),
        len(advice_scored),
        total_advice,
    )
    return {
        "false_refusal_rate_structured": false_refusal_structured,
        "advice_leakage_rate_structured": advice_leakage_structured,
    }


def main() -> int:  # noqa: C901 - the per-row error handling adds branches by nature of the fix
    ci_mode = resolve_ci_mode(sys.argv[1:])
    settings = get_settings()

    if ci_mode:
        print("=" * 78)
        print("=== CI INVARIANT GATE (EVAL_MODE=ci / --ci) ===")
        print("No LLM judge ran. No RAGAS metric ran. This checks retrieval, citation mapping,")
        print("refusal bookkeeping, and error handling against a small fixture corpus with")
        print("deterministic stub providers -- it does NOT measure answer quality.")
        print("A green result here is NOT a passing quality eval. See docs/adr/0004-ci-baselines-")
        print("vs-aspirational-thresholds.md and eval/README.md.")
        print("=" * 78)
    else:
        # Imported here, not at module scope, so CI mode never even imports these names, let alone
        # calls them -- a `pull_request` job with no JUDGE_API_KEY secret never reaches this line.
        # Python has function scope, not block scope, so classify_refusal/get_judge_client/
        # judge_selfcheck/score_comprehensibility/validate_judge_settings remain bound for the rest
        # of this function once this branch runs, exactly like every other name assigned here.
        from eval.judge import (
            classify_refusal,
            get_judge_client,
            judge_selfcheck,
            score_comprehensibility,
            validate_judge_settings,
        )

        validate_judge_settings(settings)

    print("\n=== Office Hours eval run ===")
    print(f"Mode: {'ci' if ci_mode else 'full'}")
    print(f"Orchestrator: {ORCHESTRATOR_URL}")
    if not ci_mode:
        print(f"Judge model requested: {settings.JUDGE_MODEL}")

    golden_rows = load_golden_set()
    print(f"Golden set: {len(golden_rows)} rows loaded from {GOLDEN_PATH}")

    judge_client = None if ci_mode else get_judge_client(settings)

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

            # Every response is required to carry a valid response_type (see
            # UnknownResponseTypeError) in BOTH modes: CI mode has no other source of
            # classification at all, and full mode needs it too, for the REPORTED-only
            # *_structured metrics computed alongside the judge's own classify_refusal (Item 2,
            # see compute_structured_refusal_rates). A missing/invalid response_type is a broken or
            # pre-Phase-4 orchestrator, never silently defaulted or skipped.
            response_type = response.get("response_type")
            try:
                structured_classification = classify_refusal_structured(response_type)
            except UnknownResponseTypeError as exc:
                record["errored"] = True
                record["error"] = build_error_record(exc, elapsed)
                row_records.append(record)
                print(
                    f"  [{i + 1}/{len(golden_rows)}] BAD RESPONSE_TYPE after {elapsed:.1f}s: "
                    f"{question[:70]!r} -> response_type={response_type!r}: {exc}"
                )
                continue

            contexts = [c["content"] for c in response["contexts"]]
            citation_stats = analyze_citations(
                answer_text, num_contexts=len(contexts), num_citations=num_citations_returned
            )

            if ci_mode:
                # No judge call at all: comprehensibility is a CI_SKIPPED_METRIC (needs the hosted
                # judge), and refusal classification IS structured_classification -- CI mode has no
                # other source of it (see the module docstring's CI-mode section). Free (no
                # network, no retry budget), so there is no failure mode to isolate here the way
                # the judge branch below has to.
                comprehensibility = None
                classification = structured_classification
            else:
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
                    # Recorded in BOTH modes (Item 2): response_type/refusal_reason are the
                    # pipeline's own raw fields; refusal_classification_structured is always
                    # classify_refusal_structured's mapping of response_type, identical to
                    # `classification` in CI mode and a separate, judge-independent instrument in
                    # full mode (see compute_structured_refusal_rates).
                    "response_type": response_type,
                    "refusal_reason": response.get("refusal_reason"),
                    "refusal_classification_structured": structured_classification,
                    "reading_grade_level": reading_grade_level(answer_text),
                    **citation_stats,
                }
            )
            row_records.append(record)
            step_label = "queried (CI mode, no judge)" if ci_mode else "queried + judged"
            print(
                f"  [{i + 1}/{len(golden_rows)}] {step_label} in {elapsed:.1f}s: "
                f"{question[:70]!r}"
            )

    if ci_mode:
        print("\nJudge model served: n/a (CI mode, no judge call is made).")
    else:
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
    #
    # CI mode never calls the judge at all, so there is nothing to check determinism of here --
    # StubLLM's own determinism is covered separately, by
    # services/orchestrator/tests/test_stub_providers.py.
    first = second = None
    determinism_row = None
    if ci_mode:
        print("\n--- Determinism check (judge_selfcheck) ---")
        print("SKIPPED (CI mode): no judge call is made in CI mode.")
    else:
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
                print(
                    f"comprehensibility run 1: {first}   run 2: {second}   identical: {identical}"
                )
                if first != second:
                    print(
                        "WARNING: temperature=0 did not produce identical scores on the same "
                        "input. Every downstream judge number in this run should be treated as "
                        "noise."
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
    #
    # CI mode never runs RAGAS at all -- run_ragas_metrics is imported lazily, right here, so a CI
    # run never imports ragas (see eval/metrics.py's module docstring and the top of this file).
    ragas_error: str | None = None
    ragas_row_failures: list[dict] = []
    if ci_mode:
        print(
            "\nSKIPPED (CI mode): faithfulness, answer_relevancy, context_precision need RAGAS "
            "and the hosted judge; neither runs in CI mode."
        )
    elif scored_records:
        from eval.metrics import run_ragas_metrics

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

    # Pipeline-derived refusal rates (Item 2): FULL MODE ONLY. CI mode's false_refusal_rate/
    # advice_leakage_rate in aggregate_stats above are ALREADY the structured, response_type-based
    # values (see the module docstring), so computing a second copy of the same number there would
    # be redundant.
    structured_refusal_stats = (
        None if ci_mode else compute_structured_refusal_rates(golden_rows, scored_records)
    )

    # --- Baselines (eval/baselines.json). CI mode must have one to gate against; a full run
    # --- treats a missing/unreadable file as "no reference available" and keeps going, since the
    # --- baseline comparison in full mode is informational only (THRESHOLDS alone gates it).
    if ci_mode:
        baselines_doc = load_baselines()
    else:
        try:
            baselines_doc = load_baselines()
        except Exception as exc:  # noqa: BLE001 - informational only in full mode, must not abort
            print(f"\nNOTE: could not load {BASELINES_PATH} for the baseline comparison: {exc}")
            baselines_doc = {}
    ci_baseline_metrics = baselines_doc.get("ci_baseline", {}).get("metrics", {})

    verdicts: dict[str, bool | None] = {}
    baseline_verdicts: dict[str, bool | None] = {}
    overall_pass = True

    # Refusal-classification bookkeeping (see CI_REFUSAL_BOOKKEEPING_GATE's comment): computed in
    # both modes (cheap, and useful in a full run's results file too) but only gated in CI mode.
    non_advice_scored_count = aggregate_stats["false_refusal_rate"]["n"]
    advice_scored_count = aggregate_stats["advice_leakage_rate"]["n"]
    unclassified_rows = sum(
        1 for r in scored_records if r.get("refusal_classification") not in ("REFUSAL", "ANSWER")
    )

    if ci_mode:
        # THRESHOLDS never gates in CI mode (see module docstring); every entry is reported as
        # None here, and the printed table below shows CI's own gate/result columns instead.
        for metric_name in THRESHOLDS:
            verdicts[metric_name] = None

        # errored_rows / empty_answer_rows have no THRESHOLDS entry (they are structural
        # invariants, not scored metrics) but are gated against the baseline the same way.
        for count_name, current_value in (
            ("errored_rows", errored_rows),
            ("empty_answer_rows", empty_answer_rows),
        ):
            gate_spec = CI_BASELINE_GATE[count_name]
            passed = compare_to_baseline(
                current_value,
                ci_baseline_metrics.get(count_name),
                higher_is_better=gate_spec["higher_is_better"],
                tolerance=gate_spec["tolerance"],
            )
            baseline_verdicts[count_name] = passed
            overall_pass = overall_pass and bool(passed)

        # Refusal-classification bookkeeping: gated in place of the rate values themselves (see
        # CI_BASELINE_GATE's comment on why the rates are reported, not gated).
        for check_name, current_value in (
            ("non_advice_scored_count", non_advice_scored_count),
            ("advice_scored_count", advice_scored_count),
            ("unclassified_rows", unclassified_rows),
        ):
            gate_spec = CI_REFUSAL_BOOKKEEPING_GATE[check_name]
            passed = compare_to_baseline(
                current_value,
                ci_baseline_metrics.get(check_name),
                higher_is_better=gate_spec["higher_is_better"],
                tolerance=gate_spec["tolerance"],
            )
            baseline_verdicts[check_name] = passed
            overall_pass = overall_pass and bool(passed)

        for metric_name in THRESHOLDS:
            if metric_name in CI_SKIPPED_METRICS:
                baseline_verdicts[metric_name] = None
                continue
            value = aggregate_stats[metric_name]["value"]
            if metric_name in CI_BASELINE_GATE:
                gate_spec = CI_BASELINE_GATE[metric_name]
                passed = compare_to_baseline(
                    value,
                    ci_baseline_metrics.get(metric_name),
                    higher_is_better=gate_spec["higher_is_better"],
                    tolerance=gate_spec["tolerance"],
                )
                baseline_verdicts[metric_name] = passed
                overall_pass = overall_pass and bool(passed)
            else:
                # CI_BASELINE_REPORTED (unreferenced_citation_rate, reading_grade_level): shown
                # against the baseline for tracking, never gates.
                baseline_verdicts[metric_name] = None
    else:
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
    if ci_mode:
        print(
            f"{'metric':<30}{'value':>18}  {'n/total':>9}  {'baseline':>10}  "
            f"{'gate':>18}  {'result':>17}"
        )

        def _ci_result_str(passed: bool | None) -> str:
            if passed is None:
                return "MISSING BASELINE"
            return "PASS" if passed else "FAIL"

        for metric_name in THRESHOLDS:
            stat = aggregate_stats[metric_name]
            n_total = f"{stat['n']}/{stat['total']}"
            if metric_name in CI_SKIPPED_METRICS:
                print(
                    f"{metric_name:<30}{'SKIPPED (CI mode)':>18}  {n_total:>9}  {'n/a':>10}  "
                    f"{'SKIPPED (CI mode)':>18}  {'SKIPPED (CI mode)':>17}"
                )
                continue
            value = stat["value"]
            baseline_value = ci_baseline_metrics.get(metric_name)
            if metric_name in CI_BASELINE_GATE:
                gate_str = "GATED (baseline)"
                result_str = _ci_result_str(baseline_verdicts[metric_name])
            else:
                gate_str = "REPORTED"
                result_str = "--"
            print(
                f"{metric_name:<30}{_fmt(value):>18}  {n_total:>9}  {_fmt(baseline_value):>10}  "
                f"{gate_str:>18}  {result_str:>17}"
            )
        for count_name, current_value in (
            ("errored_rows", errored_rows),
            ("empty_answer_rows", empty_answer_rows),
        ):
            baseline_value = ci_baseline_metrics.get(count_name)
            print(
                f"{count_name:<30}{current_value:>18}  {'n/a':>9}  {_fmt(baseline_value):>10}  "
                f"{'GATED (baseline)':>18}  {_ci_result_str(baseline_verdicts[count_name]):>17}"
            )
        print(
            "\n--- Refusal-classification bookkeeping (gated ALONGSIDE the rate values above; "
            "see CI_REFUSAL_BOOKKEEPING_GATE's comment) ---"
        )
        for check_name, current_value in (
            ("non_advice_scored_count", non_advice_scored_count),
            ("advice_scored_count", advice_scored_count),
            ("unclassified_rows", unclassified_rows),
        ):
            baseline_value = ci_baseline_metrics.get(check_name)
            print(
                f"{check_name:<30}{current_value:>18}  {'n/a':>9}  {_fmt(baseline_value):>10}  "
                f"{'GATED (baseline)':>18}  {_ci_result_str(baseline_verdicts[check_name]):>17}"
            )
    else:
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

        # --- Pipeline-derived refusal metrics (Item 2, full mode only): computed from each row's
        # --- own response_type via classify_refusal_structured, printed alongside (never in place
        # --- of) the judge-derived false_refusal_rate/advice_leakage_rate above. REPORTED only,
        # --- never gated -- having both instruments visible IS the point: where the judge and the
        # --- pipeline disagree, that disagreement is information about the judge, the guardrail,
        # --- or both, not noise to average away (see compute_structured_refusal_rates).
        print(
            "\n--- Pipeline-derived refusal metrics (reported only, never gated -- see "
            "compute_structured_refusal_rates) ---"
        )
        print(f"{'metric':<35}{'value':>10}  {'n/total':>9}")
        for metric_name, stat in structured_refusal_stats.items():
            n_total = f"{stat['n']}/{stat['total']}"
            print(f"{metric_name:<35}{_fmt(stat['value']):>10}  {n_total:>9}")

        # --- Baseline comparison against past full runs (informational only; a full run's exit
        # --- code is decided by THRESHOLDS alone, unchanged from Phase 1). Two references, both
        # --- printed when present: "full_run_reference" is the fixed Phase 1 run of record,
        # --- labelled historical and never updated; "full_run_reference_current" is the latest
        # --- full run (Phase 3 as of this comment) kept current as new full runs are recorded --
        # --- see eval/baselines.json's own comment on each key for what moved and why.
        for reference_key, label in (
            ("full_run_reference", "Phase 1 historical reference"),
            ("full_run_reference_current", "current reference"),
        ):
            reference = baselines_doc.get(reference_key, {})
            reference_metrics = reference.get("metrics", {})
            if not reference_metrics:
                continue
            print(f"\n--- Baseline comparison: {label} (informational only, never gates a run) ---")
            print(f"Reference: {reference.get('source', 'unknown')}")
            print(f"{'metric':<30}{'value':>10}  {'reference':>10}  {'delta':>10}")
            for metric_name, ref_value in reference_metrics.items():
                value = aggregate_stats.get(metric_name, {}).get("value")
                if value is None or ref_value is None:
                    delta_str = "n/a"
                else:
                    delta_str = f"{value - ref_value:+.3f}"
                print(f"{metric_name:<30}{_fmt(value):>10}  {_fmt(ref_value):>10}  {delta_str:>10}")
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
        "mode": "ci" if ci_mode else "full",
        "skipped_metrics": list(CI_SKIPPED_METRICS) if ci_mode else [],
        "run_at_utc": run_timestamp,
        "orchestrator_url": ORCHESTRATOR_URL,
        "judge_model_requested": None if ci_mode else settings.JUDGE_MODEL,
        "judge_model_served": served_model,
        "thresholds": THRESHOLDS,
        "total_rows": len(golden_rows),
        "scored_rows": len(scored_records),
        "errored_rows": errored_rows,
        "empty_answer_rows": empty_answer_rows,
        "non_advice_scored_count": non_advice_scored_count,
        "advice_scored_count": advice_scored_count,
        "unclassified_rows": unclassified_rows,
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
                "response_type": r.get("response_type"),
                "refusal_reason": r.get("refusal_reason"),
                "refusal_classification": r.get("refusal_classification"),
                "refusal_classification_structured": r.get("refusal_classification_structured"),
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
        # Full mode only (Item 2); empty in CI mode, where aggregate_stats' own
        # false_refusal_rate/advice_leakage_rate are already the structured values.
        "structured_refusal_rates": structured_refusal_stats or {},
        "citation_detail": citation_detail,
        "ragas_error": ragas_error,
        "ragas_row_failures": ragas_row_failures,
        "verdicts": verdicts,
        "baseline_verdicts": baseline_verdicts if ci_mode else {},
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
