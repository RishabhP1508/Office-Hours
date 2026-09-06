"""Unit tests for eval/run.py's CI-mode-only logic: the deterministic refusal detector, the
baseline comparison helper, and mode resolution -- none of these need a live stack, a database, or
a judge.

Imports `eval.run` directly; conftest.py in this directory makes that importable under a bare
`pytest` invocation regardless of how far this tests/ directory sits from the real `eval/` package
on disk (see conftest.py's own docstring).
"""

import subprocess
import sys
from pathlib import Path

import pytest

import eval.run as run


@pytest.mark.parametrize(
    ("response_type", "expected"),
    [
        ("answer", "ANSWER"),
        ("refusal_advice", "REFUSAL"),
        ("clarify", "REFUSAL"),
        ("no_answer", "REFUSAL"),
        ("blocked_unverified", "REFUSAL"),
    ],
)
def test_classify_refusal_structured_maps_every_response_type(response_type, expected):
    """Every app/schemas.py::ResponseType member maps to exactly one of REFUSAL/ANSWER -- see
    _RESPONSE_TYPE_TO_REFUSAL's own comment in eval/run.py for why the four non-ANSWER types all
    collapse to REFUSAL and what that collapse costs.
    """
    assert run.classify_refusal_structured(response_type) == expected


def test_classify_refusal_structured_rejects_an_unknown_response_type():
    """A response_type outside the five known members must raise, never default to ANSWER or
    silently pass through -- see UnknownResponseTypeError's own docstring.
    """
    with pytest.raises(run.UnknownResponseTypeError):
        run.classify_refusal_structured("some_future_value_this_eval_does_not_know_about")


def test_classify_refusal_structured_rejects_a_missing_response_type():
    """A `None` response_type (a /query response with no response_type field at all, e.g. from an
    orchestrator that predates Phase 4) must raise, never default to ANSWER.
    """
    with pytest.raises(run.UnknownResponseTypeError):
        run.classify_refusal_structured(None)


def test_compare_to_baseline_higher_is_better_pass():
    assert run.compare_to_baseline(0.80, 0.75, higher_is_better=True, tolerance=0.0) is True


def test_compare_to_baseline_higher_is_better_fail():
    assert run.compare_to_baseline(0.70, 0.75, higher_is_better=True, tolerance=0.0) is False


def test_compare_to_baseline_higher_is_better_within_tolerance_passes():
    assert run.compare_to_baseline(0.749, 0.75, higher_is_better=True, tolerance=0.01) is True


def test_compare_to_baseline_lower_is_better_pass():
    assert run.compare_to_baseline(0.10, 0.20, higher_is_better=False, tolerance=0.0) is True


def test_compare_to_baseline_lower_is_better_fail():
    assert run.compare_to_baseline(0.30, 0.20, higher_is_better=False, tolerance=0.0) is False


def test_compare_to_baseline_missing_current_value_is_none():
    assert run.compare_to_baseline(None, 0.20, higher_is_better=False, tolerance=0.0) is None


def test_compare_to_baseline_missing_baseline_value_is_none():
    assert run.compare_to_baseline(0.20, None, higher_is_better=False, tolerance=0.0) is None


def test_resolve_ci_mode_from_cli_flag():
    assert run.resolve_ci_mode(["--ci"]) is True


def test_resolve_ci_mode_defaults_to_false(monkeypatch):
    monkeypatch.delenv("EVAL_MODE", raising=False)
    assert run.resolve_ci_mode([]) is False


def test_resolve_ci_mode_from_env_var(monkeypatch):
    monkeypatch.setenv("EVAL_MODE", "ci")
    assert run.resolve_ci_mode([]) is True


def test_resolve_ci_mode_env_var_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("EVAL_MODE", "CI")
    assert run.resolve_ci_mode([]) is True


def test_thresholds_are_unchanged():
    """THRESHOLDS is the fixed, Phase 4 aspirational target (see CLAUDE.md's anti-gaming rules);
    this is a change-detector so an accidental (or deliberate) edit to a value, an operator, or a
    gated flag shows up as a failing test instead of a silent drift.
    """
    assert run.THRESHOLDS == {
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


def test_ci_skipped_metrics_are_all_gated_thresholds():
    """Every metric CI mode skips must be a real, gated THRESHOLDS entry -- if this ever drifts,
    CI mode would silently stop reporting a metric the aspirational gate still cares about.
    """
    for metric_name in run.CI_SKIPPED_METRICS:
        assert metric_name in run.THRESHOLDS
        assert run.THRESHOLDS[metric_name]["gated"] is True


def test_ci_baseline_gate_metrics_have_no_overlap_with_skipped_metrics():
    assert set(run.CI_BASELINE_GATE) & set(run.CI_SKIPPED_METRICS) == set()


def test_refusal_rates_are_gated_in_ci_mode():
    """false_refusal_rate and advice_leakage_rate ARE in CI_BASELINE_GATE as of Phase 4 step 3: the
    classification underlying them now comes from app/guardrails/classifier.py's rule layer (real
    production code, read through the response's structured response_type field), not from a
    stub-only heuristic tuned to agree with the golden set's is_advice label -- the tautology that
    used to make gating them meaningless is gone. See CI_BASELINE_GATE's own comment and
    docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md's "Superseded" section.
    """
    expected_spec = {"higher_is_better": False, "tolerance": 0}
    assert "false_refusal_rate" in run.CI_BASELINE_GATE
    assert "advice_leakage_rate" in run.CI_BASELINE_GATE
    assert run.CI_BASELINE_GATE["false_refusal_rate"] == expected_spec
    assert run.CI_BASELINE_GATE["advice_leakage_rate"] == expected_spec


def test_refusal_bookkeeping_gate_has_no_overlap_with_skipped_metrics():
    """The bookkeeping gate (denominators + classification coverage) is gated ALONGSIDE
    CI_BASELINE_GATE now (both false_refusal_rate/advice_leakage_rate and the bookkeeping counts are
    gated), so the only overlap that would make a single check's PASS/FAIL ambiguous is with
    CI_SKIPPED_METRICS (metrics CI mode never computes at all).
    """
    bookkeeping = set(run.CI_REFUSAL_BOOKKEEPING_GATE)
    assert bookkeeping & set(run.CI_SKIPPED_METRICS) == set()
    assert bookkeeping == {"non_advice_scored_count", "advice_scored_count", "unclassified_rows"}


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "eval" / "__init__.py").is_file():
            return candidate
    raise RuntimeError(f"could not find a directory containing eval/__init__.py above {start}")


# Every package the eval-ci extra (services/orchestrator/pyproject.toml) deliberately does NOT
# install. Merely importing eval.run -- what `python -m eval.run --ci` does before it ever branches
# on ci_mode -- must never require any of these, even transitively through eval.judge or
# eval.metrics (see both modules' lazy-import docstrings). ragas was the module actually named in
# the traceback that broke the first real PR run; langchain_core is what actually broke it
# (eval/judge.py imported InMemoryRateLimiter at module scope) while this test's earlier version
# only probed for ragas and missed it -- langchain and langchain_openai are included too since
# either one leaking would be the same class of bug.
_FORBIDDEN_CI_MODULES = ("ragas", "langchain_core", "langchain", "langchain_openai")


def test_importing_eval_run_never_imports_ragas_or_langchain():
    """CI mode's dependency set (services/orchestrator/pyproject.toml's `eval-ci` extra) installs
    only openai and textstat -- never ragas, langchain-core, langchain, or langchain-openai.
    Merely importing eval.run must never require any of _FORBIDDEN_CI_MODULES, even transitively.
    Run in a fresh subprocess, not the current process, since some other already-imported module in
    this same pytest session could have pulled one of these in first and mask a regression here.
    """
    repo_root = _find_repo_root(Path(__file__).resolve())
    probe = (
        "import eval.run, sys\n"
        f"forbidden = {_FORBIDDEN_CI_MODULES!r}\n"
        "leaked = [m for m in forbidden if m in sys.modules]\n"
        "print('LEAKED:' + ','.join(leaked))\n"
        "sys.exit(1 if leaked else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"importing eval.run leaked a forbidden module: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
