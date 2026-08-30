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

import eval.run as run


def test_classify_refusal_ci_detects_refusal_phrasing():
    answer = (
        "Talk to your designated school official (DSO) or a licensed immigration attorney about "
        "your specific situation."
    )
    assert run.classify_refusal_ci(answer) == "REFUSAL"


def test_classify_refusal_ci_defaults_to_answer_for_plain_factual_text():
    answer = "The STEM OPT extension is 24 months, added on top of the initial 12-month period."
    assert run.classify_refusal_ci(answer) == "ANSWER"


def test_classify_refusal_ci_is_case_insensitive():
    answer = "LICENSED IMMIGRATION ATTORNEY can advise you on that."
    assert run.classify_refusal_ci(answer) == "REFUSAL"


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


def test_refusal_rates_are_reported_stub_derived_not_gated():
    """false_refusal_rate and advice_leakage_rate must never be in CI_BASELINE_GATE: in CI mode
    they measure whether StubLLM's own advice-detection patterns agree with the golden set's
    is_advice label, not the real system's refusal quality, so gating on them would reward tuning
    the stub to match the label rather than measuring anything real. See CI_BASELINE_GATE's own
    comment and docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md.
    """
    assert "false_refusal_rate" not in run.CI_BASELINE_GATE
    assert "advice_leakage_rate" not in run.CI_BASELINE_GATE
    assert "false_refusal_rate" in run.CI_STUB_DERIVED_REPORTED_METRICS
    assert "advice_leakage_rate" in run.CI_STUB_DERIVED_REPORTED_METRICS


def test_refusal_bookkeeping_gate_has_no_overlap_with_anything_else():
    """The bookkeeping gate (denominators + classification coverage) stands in for the rate values
    above; it must never double up with CI_BASELINE_GATE, CI_SKIPPED_METRICS, or
    CI_STUB_DERIVED_REPORTED_METRICS, which would make a single check's PASS/FAIL ambiguous about
    what actually failed.
    """
    bookkeeping = set(run.CI_REFUSAL_BOOKKEEPING_GATE)
    assert bookkeeping & set(run.CI_BASELINE_GATE) == set()
    assert bookkeeping & set(run.CI_SKIPPED_METRICS) == set()
    assert bookkeeping & set(run.CI_STUB_DERIVED_REPORTED_METRICS) == set()
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
