"""Phase 5 freshness tests: the pure diff/classify functions, the DB bookkeeping helpers, the
freshness-in-answers guardrail, and (where the `[freshness]` extra is installed) the LangGraph
refresh graph itself.

Pure-function and freshness-guardrail tests below run with NO langgraph installed at all -- they
import only app.recrawl's pure functions and app.guardrails.freshness, neither of which imports
langgraph at module scope (see app/recrawl.py's own module docstring). Graph tests are marked
`requires_langgraph` (skipped where the extra is not installed) and `@pytest.mark.freshness`.

DB-backed tests use DATABASE_URL from the environment, the same convention
tests/test_guardrails.py and tests/test_hybrid_retrieval.py use: point it at a scratch database
(`officehours_freshness` or `officehours_fixtures`), never at the live 221-chunk `officehours`
corpus. Every DB-writing test here (anything using the `conn` fixture: touch_last_verified/
reindex_source directly, and every `requires_langgraph` graph test) inserts/deletes rows under a
`source_url` unique to that test and cleans up after itself -- and, before doing either, the `conn`
fixture itself refuses loudly to run at all if DATABASE_URL points at a database that already holds
a row for every URL in data/sources/sources.yaml (see
`_refuse_if_target_is_the_fully_ingested_real_corpus` below), since that is the signature of the
real, fully-ingested corpus. Read-only tests (the `pool` fixture; everything in
test_hybrid_retrieval.py) are not subject to that guard and may use `officehours_fixtures`'s
already-ingested rows, or the live corpus, read-only.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
import yaml
from pgvector import Vector
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row

from app.config import Settings, get_settings
from app.db import RetrievedChunk, hybrid_search, make_pool
from app.guardrails.freshness import (
    build_freshness,
    freshness_notice_text,
    source_health_state,
)
from app.ingest import (
    FetchedPage,
    _embed_and_store,
    load_snapshot,
    mint_snapshot_filename,
    render_snapshot,
)
from app.pipeline import answer_question
from app.providers.embeddings import OllamaEmbedder, StubEmbedder
from app.providers.llm import LLM, StubLLM
from app.recrawl import (
    GoldenImpact,
    RefreshReport,
    SourceResult,
    _short_source_label,
    classify_change,
    golden_impact_for_source,
    load_golden_source_urls,
    make_conn_factory,
    normalize_for_diff,
    record_source_failure,
    reindex_source,
    run_refresh,
    touch_last_verified,
)
from app.schemas import ResponseType


def _has_langgraph_checkpoint_sqlite() -> bool:
    """Whether `langgraph.checkpoint.sqlite.aio` -- the module `run_refresh` actually imports,
    inside `_drive` -- is importable. Deliberately NOT `find_spec("langgraph") is None`: bare
    `langgraph` is ALREADY installed in the image that serves /query, because the `eval` extra
    pins `langchain==1.3.18`, and langchain 1.3.18 itself requires `langgraph<1.3.0,>=1.2.11` as
    one of its own dependencies (`pip show langchain` inside that image lists it). So
    `find_spec("langgraph") is None` is False there, and a skipif guarded on that alone would let
    these tests run against an image that has langgraph but not the separate
    `langgraph-checkpoint-sqlite` package the `[freshness]` extra adds on top -- and die with
    `ModuleNotFoundError: No module named 'langgraph.checkpoint.sqlite'` instead of skipping.
    Do NOT "simplify" this back to `find_spec("langgraph")`; that is the bug this predicate fixes.
    """
    try:
        return importlib.util.find_spec("langgraph.checkpoint.sqlite.aio") is not None
    except ModuleNotFoundError:
        # find_spec on a dotted name imports its parent packages first to resolve `__path__`; if
        # `langgraph` itself isn't installed at all (a plain dev/CI environment with neither the
        # `eval` nor the `[freshness]` extra), that raises instead of returning None.
        return False


requires_langgraph = pytest.mark.skipif(
    not _has_langgraph_checkpoint_sqlite(), reason="needs the [freshness] extra"
)


# =================================================================================================
# PURE: normalize_for_diff / classify_change (no langgraph, no DB)
# =================================================================================================


def test_classify_change_unchanged_for_whitespace_and_last_updated_line_diff():
    old_body = "# Heading\n\nSome content here.\n\nLast updated: August 27, 2026\n"
    new_body = "# Heading\n\n  Some   content   here.  \n\nLast updated: September 1, 2026\n"

    verdict = classify_change(old_body, new_body)

    assert verdict.status == "unchanged"
    assert verdict.reason == "identical_after_normalization"
    assert verdict.added == []
    assert verdict.removed == []
    assert verdict.highlights == []


def test_classify_change_reordered_body_is_cosmetic():
    old_body = "# Heading\n\n## Section\n\nThe extension is 24 months.\n"
    new_body = "# Heading\n\nThe extension is 24 months.\n\n## Section\n"

    verdict = classify_change(old_body, new_body)

    assert verdict.status == "cosmetic"
    assert verdict.reason == "reordered_only"


def test_classify_change_link_line_reformatting_is_cosmetic():
    """ "An added navigation link line" is read here as: a line that already references another
    page gets reformatted with markdown link syntax and different punctuation/case, with no actual
    words changing -- classify_change's step 4 (aggressive normalization) is what makes this
    cosmetic rather than meaningful. A genuinely NEW line that survives normalize_for_diff's
    boilerplate filter (i.e. is not dropped outright) can never classify cosmetic under step 4
    unless something else in the diff was removed to match it: that is the deliberate "any real
    add/removal of a non-boilerplate line is meaningful" bias classify_change's own docstring
    states, and this test does not weaken it -- it exercises the one case where a line involving a
    link genuinely is cosmetic: reformatting, not addition.
    """
    # The link target is deliberately empty (`()`) so this isolates exactly the case/punctuation
    # difference the aggressive-normalization step is designed to catch. A real new URL's own
    # characters would be a genuine content addition, correctly classified meaningful instead.
    old_body = "# Heading\n\n" "- Federal Register notice containing the final rule.\n"
    new_body = "# Heading\n\n" "- [FEDERAL REGISTER NOTICE]() containing the final rule!\n"

    verdict = classify_change(old_body, new_body)

    assert verdict.status == "cosmetic"
    assert verdict.reason == "formatting_only"


def test_classify_change_added_navigation_link_line_is_unchanged():
    """The real "added navigation link line" case the module docstring's `_is_boilerplate_line`
    exists for: a genuinely NEW line that is only a markdown link (a bare "read more" / "skip to
    section" style link, nothing else on the line) never reaches classify_change's comparison at
    all -- normalize_for_diff drops it as boilerplate on the new side before either side is
    diffed. classify_change is the function `_after_diff` actually routes on, so this is asserted
    directly against it (not only against normalize_for_diff's own line-list output, which
    test_normalize_for_diff_drops_boilerplate_and_collapses_whitespace below already covers).
    """
    old_body = "# Heading\n\nSome content here.\n"
    new_body = "# Heading\n\nSome content here.\n\n[Read more](https://example.gov/more)\n"

    verdict = classify_change(old_body, new_body)

    assert verdict.status == "unchanged"
    assert verdict.reason == "identical_after_normalization"


@pytest.mark.full_corpus
def test_classify_change_meaningful_for_a_real_rule_edit_with_changed_number_in_highlights():
    """Uses the real Phase 5 snapshot (full_corpus-marked: this exact file is not part of the
    small CI fixture corpus, the same reason test_chunking.py's real-corpus-only assertions are
    marked full_corpus).
    """
    snapshot_path = (
        Path(get_settings().RAW_SNAPSHOT_DIR)
        / "fixed_admission-studyinthestates-final-rule-quick-facts.md"
    )
    _, old_body = load_snapshot(snapshot_path)
    assert "30-day period for departure" in old_body, "fixture assumption changed; update this test"
    new_body = old_body.replace("30-day period for departure", "45-day period for departure")

    verdict = classify_change(old_body, new_body)

    assert verdict.status == "meaningful"
    assert verdict.reason == "content_lines_changed"
    assert "45-day" in verdict.highlights


def test_classify_change_highlights_never_influence_the_verdict():
    """A real word-for-word content change with nothing highlight-worthy (no number+unit, dollar
    amount, date, form number, or rule-shaped keyword) still classifies meaningful -- `highlights`
    is explanatory only, per ChangeVerdict's own docstring.
    """
    old_body = "# Heading\n\nThe cat sat on the mat.\n"
    new_body = "# Heading\n\nThe dog sat on the mat.\n"

    verdict = classify_change(old_body, new_body)

    assert verdict.status == "meaningful"
    assert verdict.highlights == []


def test_normalize_for_diff_drops_boilerplate_and_collapses_whitespace():
    body = (
        "\n\n[Skip to main content](#main)\n\n"
        "# Heading\n\n"
        "Some    content   here.\n\n"
        "Last Reviewed/Updated: 08/27/2026\n\n"
        "---\n\n"
        "Return to top\n"
        "Print\n"
        "Share\n"
    )
    assert normalize_for_diff(body) == ["# Heading", "Some content here."]


def test_importing_app_recrawl_never_imports_langgraph():
    """Fresh subprocess: merely importing app.recrawl must never pull in langgraph OR langchain
    (any module whose top-level name starts with either), even if some other already-imported
    module in this pytest session happened to import one first (as test_ci_eval_mode.py's own
    equivalent guard notes for eval.run/ragas). app.recrawl imports only app.config, app.db,
    app.ingest, and stdlib -- asserting the whole class (not just the bare `langgraph` name) is
    what catches a langchain-core leak arriving by some other route. Uses the exact same predicate
    source as test_ci_eval_mode.py's serving-path guard
    (conftest.LANGGRAPH_OR_LANGCHAIN_PREDICATE_SRC) so the two guards cannot drift apart.
    """
    from conftest import LANGGRAPH_OR_LANGCHAIN_PREDICATE_SRC

    probe = (
        "import app.recrawl, sys\n"
        f"leaked = sorted(m for m in sys.modules if {LANGGRAPH_OR_LANGCHAIN_PREDICATE_SRC})\n"
        "print('LEAKED:' + ','.join(leaked))\n"
        "sys.exit(1 if leaked else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"importing app.recrawl leaked langgraph/langchain: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# =================================================================================================
# PURE: RefreshReport / SourceResult -- the report's own plumbing (no langgraph, no DB)
# =================================================================================================


def _make_source_result(**overrides) -> SourceResult:
    defaults = dict(
        source_url="https://example.gov/report-test",
        status="meaningful",
        reason="content_lines_changed",
        resumed=False,
        fetched_this_run=True,
        chunks_indexed=3,
        attempts=1,
        node_trail=["fetch", "diff", "chunk", "embed", "reindex"],
    )
    defaults.update(overrides)
    return SourceResult(**defaults)


def test_refresh_report_to_dict_carries_sample_lines_and_fetch_failed_serializes_safely():
    """Drives RefreshReport/SourceResult directly (not through the graph) -- this section tests the
    report's own plumbing, which R2's graph test below already proves is fed real verdict evidence.
    """
    changed = _make_source_result(
        source_url="https://example.gov/report-test-changed",
        added=["The extension period was updated to a longer duration."],
        removed=["The extension period was previously a shorter duration."],
        added_count=1,
        removed_count=1,
        highlights=["45-day"],
    )
    failed = _make_source_result(
        source_url="https://example.gov/report-test-failed",
        status="fetch_failed",
        reason="max_attempts_exceeded",
        fetched_this_run=False,
        chunks_indexed=0,
        attempts=3,
        node_trail=["fetch", "fetch", "fetch", "record_failure"],
        # no verdict at all for a fetch_failed source -- added/removed/highlights stay at their
        # dataclass defaults, exactly as _verdict_evidence(None) produces.
    )
    report = RefreshReport(run_id="test-report", results=[changed, failed], http_fetches_this_run=2)

    payload = report.to_dict()
    by_url = {r["source_url"]: r for r in payload["results"]}

    changed_payload = by_url[changed.source_url]
    assert changed_payload["added"] == ["The extension period was updated to a longer duration."]
    assert changed_payload["removed"] == ["The extension period was previously a shorter duration."]
    assert changed_payload["added_count"] == 1
    assert changed_payload["removed_count"] == 1
    assert changed_payload["highlights"] == ["45-day"]

    failed_payload = by_url[failed.source_url]
    assert failed_payload["added"] == []
    assert failed_payload["removed"] == []
    assert failed_payload["added_count"] == 0
    assert failed_payload["removed_count"] == 0
    assert failed_payload["highlights"] == []

    # This IS the artifact the scheduled job uploads -- it must actually serialize, including the
    # fetch_failed row with no verdict at all.
    serialized = json.dumps(payload)
    assert "The extension period was updated to a longer duration." in serialized
    assert "max_attempts_exceeded" in serialized

    # render_table must not crash on the fetch_failed source either. It DOES show the highlight
    # token (that's the "at a glance" summary the table is for), but never the full sample lines --
    # that is what --json is for.
    table = report.render_table()
    assert "45-day" in table
    assert "The extension period was updated to a longer duration." not in table
    assert "The extension period was previously a shorter duration." not in table
    assert "content_lines_changed" in table
    assert "max_attempts_exceeded" in table


def test_render_table_shows_counts_and_highlights_for_changed_sources_only():
    changed = _make_source_result(
        source_url="https://example.gov/report-test-meaningful",
        added_count=1,
        removed_count=1,
        highlights=["45-day"],
    )
    unchanged = _make_source_result(
        source_url="https://example.gov/report-test-unchanged",
        status="unchanged",
        reason="identical_after_normalization",
        resumed=True,
        fetched_this_run=False,
        chunks_indexed=0,
        node_trail=["fetch", "diff", "verify_only"],
    )
    report = RefreshReport(
        run_id="test-report", results=[changed, unchanged], http_fetches_this_run=2
    )

    table = report.render_table()
    lines = table.split("\n")
    changed_line = next(line for line in lines if "report-test-meaningful" in line)
    unchanged_line = next(line for line in lines if "report-test-unchanged" in line)

    assert "+1/-1" in changed_line
    assert "45-day" in changed_line
    # unchanged never shows a +added/-removed count or highlights, even though the dataclass
    # default (0/0/[]) would render as "+0/-0" if this guard were missing.
    assert "+0/-0" not in unchanged_line
    assert "45-day" not in unchanged_line


def test_render_table_status_column_lines_up_for_the_longest_manifest_url():
    """Column alignment is pure string formatting on whatever `source_url`s a `SourceResult`
    carries -- it does not need the real manifest, and reading it through
    `Settings.SOURCES_MANIFEST_PATH` only works inside the orchestrator's Docker image (the
    default is `/app/data/sources/sources.yaml`), which does not exist on a CI runner. That is
    also why this is the one test in this file that is NOT marked `full_corpus`: unlike
    test_chunking.py's real-corpus assertions, this behavior is exercisable, and should be
    checked, without the real 14-source corpus.

    `longest_url` below is built at least as long as the actual longest manifest entry (the
    fixed-admission FAQ URL, ~145 characters) so this stays a real stress case for
    `render_table`'s column sizing rather than a stand-in shorter than what it actually has to
    handle.
    """
    longest_url = (
        "https://studyinthestates.dhs.gov/final-rule-establishing-a-fixed-time-period-of-admission"
        "-and-an-extension-of-stay-procedure-faq-for-testing-column-alignment-with-a-very-long-url"
    )
    shortest_url = "https://example.gov/faq"
    assert len(longest_url) >= 145
    assert longest_url != shortest_url

    long_row = _make_source_result(
        source_url=longest_url,
        added=["some new line"],
        removed=["some old line"],
        added_count=1,
        removed_count=1,
        highlights=["45-day"],
    )
    short_row = _make_source_result(
        source_url=shortest_url,
        status="unchanged",
        reason="identical_after_normalization",
        resumed=True,
        fetched_this_run=False,
        chunks_indexed=0,
        node_trail=["fetch", "diff", "verify_only"],
    )
    report = RefreshReport(
        run_id="test-report", results=[long_row, short_row], http_fetches_this_run=2
    )

    table = report.render_table()

    source_width = max(
        len("source"),
        len(_short_source_label(longest_url)),
        len(_short_source_label(shortest_url)),
    )
    expected_status_col = source_width + 1

    lines = table.split("\n")
    long_line = next(line for line in lines if _short_source_label(longest_url) in line)
    short_line = next(line for line in lines if _short_source_label(shortest_url) in line)

    assert long_line[expected_status_col:].split()[0] == "meaningful"
    assert short_line[expected_status_col:].split()[0] == "unchanged"

    # Sample lines never appear in the table, no matter how the columns are sized.
    assert "some new line" not in table
    assert "some old line" not in table
    assert "Counts:" in table
    assert "HTTP fetches this run: 2" in table


# =================================================================================================
# PURE: golden-set impact reporting (Phase 8 round 2, B1/B2 -- no DB, no langgraph). Read-only:
# every test in this section only ever reads a golden.jsonl-shaped file, real or synthetic; none
# writes to or edits eval/golden.jsonl.
# =================================================================================================

# Independently-computed ground truth (Phase 8 round 2 brief) for THIS project's real
# eval/golden.jsonl: which of its 14 manifest sources are cited by which golden row indices
# (0-based, matching golden.jsonl's own line order). The 8 sources absent from this dict are cited
# by no golden row at all -- see _UNCITED_REAL_SOURCES below.
_EXPECTED_GOLDEN_IMPACT_BY_SOURCE = {
    "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/"
    "optional-practical-training-extension-for-stem-students-stem-opt": [
        0,
        1,
        4,
        11,
        12,
        18,
        19,
        20,
    ],
    "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/"
    "optional-practical-training-opt-for-f-1-students": [2, 3, 9, 10, 18],
    "https://www.uscis.gov/working-in-the-united-states/temporary-workers/"
    "h-1b-specialty-occupations/h-1b-electronic-registration-process": [5, 6, 14, 15],
    "https://www.uscis.gov/working-in-the-united-states/temporary-workers/"
    "h-1b-specialty-occupations/h-1b-cap-season": [7, 13, 16],
    "https://www.uscis.gov/working-in-the-united-states/h-1b-specialty-occupations": [
        8,
        17,
    ],
    "https://studyinthestates.dhs.gov/sevis-help-hub/student-records/fm-student-employment/"
    "f-1-optional-practical-training-opt": [19],
}

_UNCITED_REAL_SOURCES = [
    "https://www.uscis.gov/working-in-the-united-states/temporary-workers/"
    "h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-"
    "f-1-status-for-eligible-students",
    "https://studyinthestates.dhs.gov/students/complete/h-1b-status-and-the-cap-gap-extension",
    "https://studyinthestates.dhs.gov/students/maintaining-status",
    "https://studyinthestates.dhs.gov/students/study/full-course-of-study",
    "https://studyinthestates.dhs.gov/students/study/traveling-as-an-international-student",
    "https://www.ice.gov/sevis/travel",
    "https://studyinthestates.dhs.gov/final-rule-establishing-a-fixed-time-period-of-admission-"
    "and-an-extension-of-stay-procedure-faq",
    "https://studyinthestates.dhs.gov/final-rule-establishing-a-fixed-time-period-of-admission-"
    "and-an-extension-of-stay-procedure-quick",
]

# eval/golden.jsonl relative to this test file: tests -> orchestrator -> services -> repo root.
_REAL_GOLDEN_PATH = Path(__file__).resolve().parents[3] / "eval" / "golden.jsonl"


def test_golden_impact_reproduces_the_real_golden_jsonl_ground_truth():
    """Reproduces, exactly, the independently-computed ground truth for the real, hand-authored
    eval/golden.jsonl (21 rows): which manifest sources each row cites, matched by source_url.
    Read-only -- this loads golden.jsonl through load_golden_source_urls and never writes to it.
    """
    golden_rows = load_golden_source_urls(_REAL_GOLDEN_PATH)
    assert golden_rows is not None
    assert len(golden_rows) == 21

    for source_url, expected_indices in _EXPECTED_GOLDEN_IMPACT_BY_SOURCE.items():
        impact = golden_impact_for_source(
            golden_rows,
            golden_path=_REAL_GOLDEN_PATH,
            source_url=source_url,
            resolved_url=None,
        )
        assert impact.available is True
        assert impact.affected_indices == expected_indices, source_url

    # The negative case: a meaningful change to one of the 8 sources no golden row cites reports an
    # empty affected list -- not a crash, not a false positive.
    for source_url in _UNCITED_REAL_SOURCES:
        impact = golden_impact_for_source(
            golden_rows,
            golden_path=_REAL_GOLDEN_PATH,
            source_url=source_url,
            resolved_url=None,
        )
        assert impact.available is True
        assert impact.affected_indices == [], source_url


def test_golden_impact_for_source_matches_resolved_url_not_only_manifest_url():
    """One manifest entry redirects (traveling-as-an-international-student ->
    traveling-as-an-f-or-m-student), so a golden row could cite either form. Matching on
    source_url alone would silently miss a row that cites only the resolved form.
    """
    golden_rows = [
        {"https://example.gov/old-name"},  # cites the pre-redirect manifest URL
        {"https://example.gov/new-name"},  # cites the POST-redirect URL only
        {"https://example.gov/unrelated"},
    ]

    both = golden_impact_for_source(
        golden_rows,
        golden_path=Path("unused"),
        source_url="https://example.gov/old-name",
        resolved_url="https://example.gov/new-name",
    )
    assert both.available is True
    assert both.affected_indices == [0, 1]

    manifest_url_only = golden_impact_for_source(
        golden_rows,
        golden_path=Path("unused"),
        source_url="https://example.gov/old-name",
        resolved_url=None,
    )
    assert manifest_url_only.affected_indices == [0]  # row 1 would be silently missed


def test_load_golden_source_urls_returns_none_when_file_missing(tmp_path):
    assert load_golden_source_urls(tmp_path / "does-not-exist.jsonl") is None


def test_load_golden_source_urls_returns_none_on_malformed_json(tmp_path):
    path = tmp_path / "malformed.jsonl"
    path.write_text('{"source_urls": ["https://x"]}\nNOT JSON\n', encoding="utf-8")
    assert load_golden_source_urls(path) is None


def test_load_golden_source_urls_returns_none_when_source_urls_field_is_missing_or_malformed(
    tmp_path,
):
    missing_field = tmp_path / "missing-field.jsonl"
    missing_field.write_text(json.dumps({"question": "no source_urls here"}), encoding="utf-8")
    assert load_golden_source_urls(missing_field) is None

    wrong_type = tmp_path / "wrong-type.jsonl"
    wrong_type.write_text(json.dumps({"source_urls": "not-a-list"}), encoding="utf-8")
    assert load_golden_source_urls(wrong_type) is None


def test_load_golden_source_urls_skips_blank_lines_and_preserves_order(tmp_path):
    path = tmp_path / "well-formed.jsonl"
    path.write_text(
        json.dumps({"source_urls": ["https://a"]})
        + "\n\n"
        + json.dumps({"source_urls": ["https://b", "https://c"]}),
        encoding="utf-8",
    )
    rows = load_golden_source_urls(path)
    assert rows == [{"https://a"}, {"https://b", "https://c"}]


def test_golden_impact_for_source_reports_unavailable_never_a_silent_zero(tmp_path):
    """golden_rows=None (the file could not be read/parsed) must report available=False with a
    non-empty note, NEVER an empty affected_indices list that looks identical to "checked, nothing
    affected" -- that indistinguishability is the exact failure this feature exists to prevent.
    """
    missing_path = tmp_path / "does-not-exist.jsonl"
    impact = golden_impact_for_source(
        None,
        golden_path=missing_path,
        source_url="https://example.gov/x",
        resolved_url=None,
    )
    assert impact.available is False
    assert impact.affected_indices == []
    assert impact.note is not None
    assert "unavailable" in impact.note.lower()


# =================================================================================================
# PURE: RefreshReport.exit_code / render_table / to_dict with golden_impact (Phase 8 round 2, B2)
# =================================================================================================


def test_exit_code_is_green_when_meaningful_change_is_not_cited_by_any_golden_row():
    result = _make_source_result(
        status="meaningful",
        golden_impact=GoldenImpact(available=True, affected_indices=[], note=None),
    )
    report = RefreshReport(run_id="t", results=[result], http_fetches_this_run=1)
    assert report.exit_code() == 0


def test_exit_code_is_red_when_meaningful_change_is_cited_by_golden_rows():
    result = _make_source_result(
        status="meaningful",
        golden_impact=GoldenImpact(available=True, affected_indices=[3, 5], note=None),
    )
    report = RefreshReport(run_id="t", results=[result], http_fetches_this_run=1)
    assert report.exit_code() == 1


def test_exit_code_is_red_when_golden_set_is_unavailable_for_a_meaningful_change():
    result = _make_source_result(
        status="meaningful",
        golden_impact=GoldenImpact(
            available=False, affected_indices=[], note="golden set unavailable: ..."
        ),
    )
    report = RefreshReport(run_id="t", results=[result], http_fetches_this_run=1)
    assert report.exit_code() == 1


def test_exit_code_still_red_on_fetch_failed_alone_unchanged_from_before():
    result = _make_source_result(
        status="fetch_failed", reason="max_attempts_exceeded", golden_impact=None
    )
    report = RefreshReport(run_id="t", results=[result], http_fetches_this_run=1)
    assert report.exit_code() == 1


def test_render_table_banner_names_both_red_reasons_when_both_apply():
    broken = _make_source_result(
        source_url="https://example.gov/broken",
        status="fetch_failed",
        reason="max_attempts_exceeded",
        golden_impact=None,
    )
    golden_hit = _make_source_result(
        source_url="https://example.gov/golden-hit",
        status="meaningful",
        golden_impact=GoldenImpact(available=True, affected_indices=[2], note=None),
    )
    report = RefreshReport(run_id="t", results=[broken, golden_hit], http_fetches_this_run=2)

    table = report.render_table()
    lines = table.split("\n")
    banner_lines = lines[: lines.index("")]
    banner_text = "\n".join(banner_lines)

    assert "RED RUN" in banner_text
    assert "BROKEN" in banner_text
    assert "example.gov/broken" in banner_text
    assert "golden" in banner_text.lower()
    assert "example.gov/golden-hit" in banner_text

    # The row itself also carries the golden verdict (distinguished from the banner detail line
    # above, which also mentions this same shortened label and even the word "meaningful", by
    # requiring the line to actually START with the source label -- only the table row does).
    hit_line = next(line for line in lines if line.startswith("example.gov/golden-hit"))
    assert "rows [2]" in hit_line
    assert "AFFECTED" in hit_line


def test_render_table_shows_golden_none_cited_and_no_banner_on_a_green_meaningful_change():
    result = _make_source_result(
        source_url="https://example.gov/green-meaningful",
        status="meaningful",
        golden_impact=GoldenImpact(available=True, affected_indices=[], note=None),
    )
    report = RefreshReport(run_id="t", results=[result], http_fetches_this_run=1)

    table = report.render_table()
    assert "RED RUN" not in table
    row_line = next(line for line in table.split("\n") if "green-meaningful" in line)
    assert "golden: none cited" in row_line


def test_to_dict_red_reasons_lists_the_right_source_urls_and_stays_empty_when_green():
    broken = _make_source_result(
        source_url="https://example.gov/broken2",
        status="fetch_failed",
        reason="max_attempts_exceeded",
        golden_impact=None,
    )
    golden_hit = _make_source_result(
        source_url="https://example.gov/golden-hit2",
        status="meaningful",
        golden_impact=GoldenImpact(available=True, affected_indices=[0], note=None),
    )
    clean = _make_source_result(
        source_url="https://example.gov/clean",
        status="meaningful",
        golden_impact=GoldenImpact(available=True, affected_indices=[], note=None),
    )
    report = RefreshReport(run_id="t", results=[broken, golden_hit, clean], http_fetches_this_run=3)

    payload = report.to_dict()
    assert payload["exit_code"] == 1
    assert payload["red_reasons"]["broken_sources"] == ["https://example.gov/broken2"]
    assert payload["red_reasons"]["golden_review_sources"] == ["https://example.gov/golden-hit2"]

    green_report = RefreshReport(run_id="t", results=[clean], http_fetches_this_run=1)
    green_payload = green_report.to_dict()
    assert green_payload["exit_code"] == 0
    assert green_payload["red_reasons"] == {
        "broken_sources": [],
        "golden_review_sources": [],
    }


# =================================================================================================
# DB-BACKED: touch_last_verified / reindex_source (no langgraph needed)
# =================================================================================================


def _find_repo_root_containing_manifest(start: Path) -> Path | None:
    """Search upward from `start` for the directory holding data/sources/sources.yaml -- the same
    upward-search shape conftest.py already uses to locate the top-level `eval` package. Needed
    here because Settings.SOURCES_MANIFEST_PATH defaults to a container path
    (/app/data/sources/sources.yaml) that does not exist outside the orchestrator's own Docker
    image, and these tests must be able to find the real manifest when run directly on a dev
    machine.
    """
    for candidate in (start, *start.parents):
        if (candidate / "data" / "sources" / "sources.yaml").is_file():
            return candidate
    return None


def _manifest_urls() -> set[str]:
    root = _find_repo_root_containing_manifest(Path(__file__).resolve())
    assert (
        root is not None
    ), "could not locate data/sources/sources.yaml by searching upward from this test file"
    manifest = yaml.safe_load(
        (root / "data" / "sources" / "sources.yaml").read_text(encoding="utf-8")
    )
    return {entry["url"] for entry in manifest["sources"]}


async def _refuse_if_target_is_the_fully_ingested_real_corpus(
    conn: psycopg.AsyncConnection, database_url: str
) -> None:
    """The guard every DB-WRITING test in this file runs (via the `conn` fixture below) before it
    ever inserts, updates, or deletes a row. Refuses loudly -- never a silent skip -- if
    `database_url` already holds a row for every one of the 14 URLs in data/sources/sources.yaml:
    that is the signature of the real, fully-ingested corpus (216/221 chunks as of this writing),
    which is exactly the database these tests must never write to (see
    test_touch_last_verified_moves_last_verified_and_leaves_everything_else and
    test_reindex_source_replaces_rows_and_moves_fetched_at_and_page_last_updated below, plus the
    `requires_langgraph` graph tests further down, which all write through this same `conn`
    fixture or through a conn_factory pointed at the same `database_url`).

    Keyed on "every manifest URL present as a source_url", never on a row count: a row count
    threshold would either fire on CI's small ingested fixture corpus (eval/fixtures/sources, 4 of
    the 14 manifest URLs -- which these write tests run against in CI and must NOT be blocked from)
    or fail to fire on a partially-ingested real corpus. Read-only tests (the `pool` fixture below,
    and everything in test_hybrid_retrieval.py) never call this at all.
    """
    # psycopg's default autocommit=False means a bare execute() here would otherwise leave `conn`
    # sitting in an open, uncommitted transaction for the rest of the test -- the guard is the
    # FIRST thing the `conn` fixture does, before the test body's own transaction (e.g.
    # _insert_test_row's `async with conn.transaction():`), so an uncommitted guard transaction
    # would make every write a SAVEPOINT nested inside it rather than its own top-level transaction.
    # That does not merely look wrong: a `requires_langgraph` graph test writes the SAME row again
    # from a SECOND, separate connection (run_refresh's own conn_factory), and Postgres must then
    # block that second connection on a row lock until this fixture's still-open transaction ends --
    # a real deadlock/stale-read hazard, reproduced while writing this guard. rollback() (not
    # commit(): this is a read-only check with nothing to persist) ends the transaction cleanly,
    # leaving `conn` exactly as unencumbered as it was before this guard existed.
    manifest_urls = _manifest_urls()
    async with conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT source_url FROM documents")
        rows = await cur.fetchall()
    await conn.rollback()
    present_urls = {row[0] for row in rows}
    if manifest_urls and manifest_urls <= present_urls:
        pytest.fail(
            f"Refusing to run a DB-writing test against DATABASE_URL={database_url!r}: it already "
            f"holds a row for every one of the {len(manifest_urls)} URLs in "
            "data/sources/sources.yaml, which is the signature of the real, fully-ingested corpus. "
            "This test inserts/deletes rows in `documents`. Point DATABASE_URL at a scratch "
            "database instead (for example officehours_fixtures or officehours_freshness) and "
            "re-run."
        )


@pytest.fixture
def database_url() -> str:
    return os.environ.get("DATABASE_URL", get_settings().DATABASE_URL)


@pytest.fixture
async def conn(database_url):
    connection = await psycopg.AsyncConnection.connect(database_url)
    await register_vector_async(connection)
    try:
        await _refuse_if_target_is_the_fully_ingested_real_corpus(connection, database_url)
        yield connection
    finally:
        await connection.close()


async def _insert_test_row(
    conn: psycopg.AsyncConnection,
    source_url: str,
    *,
    fetched_at: datetime,
    page_last_updated: date | None = None,
    heading: str = "Old Heading",
    content: str = "old content",
    last_indexed_body: str | None = None,
    rule_effective_date: date | None = None,
) -> int:
    """Phase 7: `resolved_url`/`page_last_updated`/`fetched_at`/`last_verified_at` moved off
    `documents` onto `sources` (the FK requires a `sources` row to exist before any `documents` row
    can reference it, so the upsert below runs first). Initializes the same "healthy, just-crawled"
    shape `_embed_and_store`'s own upsert does: last_success_at/last_changed_at := fetched_at,
    change_count/consecutive_failures := 0, status := 'ok'.

    Stateless-recrawl columns (docs/adr/0014-stateless-recrawl-diff.md): `last_indexed_body`
    defaults to None -- a `sources` row with real `documents` chunks (which this helper always
    creates) and a NULL `last_indexed_body` is exactly the post-migration, pre-backfill state
    app/recrawl.py::_diff_node refuses to silently treat as a first-time index (see
    test_graph_diff_raises_loudly_when_backfill_has_not_run below). Tests that need `_diff_node` to
    actually diff against a real prior body (every other graph test that reaches the diff at all)
    pass `last_indexed_body=` explicitly.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO sources
                    (source_url, resolved_url, page_last_updated, fetched_at, last_verified_at,
                     last_changed_at, last_success_at, change_count, consecutive_failures,
                     last_error, last_http_status, status, last_indexed_body, rule_effective_date)
                VALUES (%s, NULL, %s, %s, %s, %s, %s, 0, 0, NULL, NULL, 'ok', %s, %s)
                ON CONFLICT (source_url) DO UPDATE SET
                    resolved_url = EXCLUDED.resolved_url,
                    page_last_updated = EXCLUDED.page_last_updated,
                    fetched_at = EXCLUDED.fetched_at,
                    last_verified_at = EXCLUDED.last_verified_at,
                    last_changed_at = EXCLUDED.last_changed_at,
                    last_success_at = EXCLUDED.last_success_at,
                    change_count = 0,
                    consecutive_failures = 0,
                    last_error = NULL,
                    last_http_status = NULL,
                    status = 'ok',
                    last_indexed_body = EXCLUDED.last_indexed_body,
                    rule_effective_date = EXCLUDED.rule_effective_date
                """,
                (
                    source_url,
                    page_last_updated,
                    fetched_at,
                    fetched_at,
                    fetched_at,
                    fetched_at,
                    last_indexed_body,
                    rule_effective_date,
                ),
            )
            await cur.execute("DELETE FROM documents WHERE source_url = %s", (source_url,))
            await cur.execute(
                """
                INSERT INTO documents
                    (content, source_url, section_heading, heading_level,
                     rule_effective_date, embedding)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    content,
                    source_url,
                    heading,
                    2,
                    None,
                    Vector([0.0] * 768),
                ),
            )
            row = await cur.fetchone()
            return row[0]


async def _delete_test_rows(conn: psycopg.AsyncConnection, *source_urls: str) -> None:
    """Deletes both `documents` rows and their `sources` row -- in that order, since the FK
    (ON DELETE RESTRICT) forbids removing a `sources` row while any `documents` row still
    references it.
    """
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM documents WHERE source_url = ANY(%s)", (list(source_urls),))
        await cur.execute("DELETE FROM sources WHERE source_url = ANY(%s)", (list(source_urls),))
    await conn.commit()


async def test_touch_last_verified_moves_last_verified_and_leaves_everything_else(conn):
    source_url = "https://example.gov/freshness-test-touch-last-verified"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    old_page_last_updated = date(2026, 1, 1)

    old_body = "old content"
    row_id = await _insert_test_row(
        conn,
        source_url,
        fetched_at=old_time,
        page_last_updated=old_page_last_updated,
        last_indexed_body=old_body,
    )

    new_now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    new_rule_effective_date = date(2026, 9, 15)
    rowcount = await touch_last_verified(
        conn, source_url, now=new_now, rule_effective_date=new_rule_effective_date
    )
    assert rowcount == 1

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, content, rule_effective_date FROM documents WHERE source_url = %s",
            (source_url,),
        )
        row = await cur.fetchone()

    assert row["id"] == row_id
    assert row["content"] == "old content"
    assert row["rule_effective_date"] == new_rule_effective_date

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fetched_at, last_verified_at, page_last_updated, last_success_at, "
            "consecutive_failures, last_error, last_http_status, status, last_indexed_body, "
            "rule_effective_date FROM sources WHERE source_url = %s",
            (source_url,),
        )
        source_row = await cur.fetchone()

    # fetched_at/page_last_updated/last_indexed_body never move -- touch_last_verified is
    # bookkeeping only (see infra/sql/init.sql's comment on last_indexed_body: nothing was
    # re-indexed, so the next diff's baseline must stay exactly what it already was).
    assert source_row["fetched_at"] == old_time
    assert source_row["page_last_updated"] == old_page_last_updated
    assert source_row["last_indexed_body"] == old_body
    assert source_row["last_verified_at"] == new_now
    assert source_row["last_success_at"] == new_now
    assert source_row["consecutive_failures"] == 0
    assert source_row["last_error"] is None
    assert source_row["last_http_status"] is None
    assert source_row["status"] == "ok"
    # The curator annotation DOES sync onto `sources`, mirroring what already happens on
    # `documents` (both asserted above) -- see touch_last_verified's own docstring.
    assert source_row["rule_effective_date"] == new_rule_effective_date

    await _delete_test_rows(conn, source_url)


async def test_reindex_source_replaces_rows_and_moves_fetched_at_and_page_last_updated(
    conn,
):
    source_url = "https://example.gov/freshness-test-reindex"
    other_source_url = "https://example.gov/freshness-test-reindex-other"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    old_page_last_updated = date(2026, 1, 1)

    old_row_id = await _insert_test_row(
        conn, source_url, fetched_at=old_time, page_last_updated=old_page_last_updated
    )
    other_row_id = await _insert_test_row(
        conn,
        other_source_url,
        fetched_at=old_time,
        page_last_updated=old_page_last_updated,
        heading="Other Heading",
    )

    embedder = StubEmbedder(dim=768)
    new_now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    new_page_last_updated = date(2026, 9, 1)
    new_rule_effective_date = date(2026, 9, 15)
    new_body = "New Heading\n\nSome new content."
    chunks = [
        {
            "heading": "New Heading",
            "level": 2,
            "parent": None,
            "breadcrumb": "New Heading",
            "text": new_body,
        }
    ]

    count = await reindex_source(
        conn,
        embedder,
        source_url=source_url,
        resolved_url=None,
        page_last_updated=new_page_last_updated,
        rule_effective_date=new_rule_effective_date,
        body=new_body,
        chunks=chunks,
        now=new_now,
    )
    assert count == 1

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, section_heading, rule_effective_date FROM documents "
            "WHERE source_url = %s",
            (source_url,),
        )
        rows = await cur.fetchall()

    assert len(rows) == 1
    new_row = rows[0]
    assert new_row["id"] != old_row_id
    assert new_row["section_heading"] == "New Heading"
    assert new_row["rule_effective_date"] == new_rule_effective_date

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fetched_at, last_verified_at, page_last_updated, last_changed_at, "
            "change_count, last_indexed_body, rule_effective_date "
            "FROM sources WHERE source_url = %s",
            (source_url,),
        )
        source_row = await cur.fetchone()
    assert source_row["fetched_at"] == new_now
    assert source_row["last_verified_at"] == new_now
    assert source_row["page_last_updated"] == new_page_last_updated
    # reindex_source always passes mark_changed=True -- last_changed_at/change_count must move.
    assert source_row["last_changed_at"] == new_now
    assert source_row["change_count"] == 1
    # DoD 4: the freshly indexed body and curator annotation land on `sources` too, not just on
    # `documents` -- this is what the NEXT recrawl's stateless _diff_node reads.
    assert source_row["last_indexed_body"] == new_body
    assert source_row["rule_effective_date"] == new_rule_effective_date

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT id FROM documents WHERE source_url = %s", (other_source_url,))
        other_row = await cur.fetchone()
    assert other_row["id"] == other_row_id

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fetched_at FROM sources WHERE source_url = %s", (other_source_url,)
        )
        other_source_row = await cur.fetchone()
    assert other_source_row["fetched_at"] == old_time

    await _delete_test_rows(conn, source_url, other_source_url)


# =================================================================================================
# PHASE 7: the `sources` normalization invariant -- proven three ways (behavioral, schema, and a
# static source-guard), plus record_source_failure's own never-advance-the-clock guarantee.
# =================================================================================================


class _RaisingEmbedder:
    async def embed(self, texts):
        del texts
        raise RuntimeError("simulated embedder failure")


async def test_reindex_source_embedder_failure_leaves_existing_chunks_intact(conn):
    """DoD 1(a), BEHAVIORAL: a reindex_source call whose embedder raises must never leave a source
    half-migrated -- the pre-existing chunks must survive, unchanged, by id. This asserts the
    OBSERVABLE outcome (a fresh read from the database, not a claim about how the implementation
    gets there), so it stays true even if a future refactor changes exactly where inside
    `_embed_and_store` the embedder is called relative to the transaction.

    LIMITATION, stated plainly rather than glossed over: `_embed_and_store` calls
    `embedder.embed(texts)` BEFORE `async with conn.transaction():` opens, so this specific failure
    never reaches the DELETE at all -- it is a fine regression test for "an embedder error must
    never corrupt a source," but it does NOT exercise a real ROLLBACK, and would pass unchanged even
    if the DELETE/INSERT were not transactional at all. The next test (
    `test_reindex_source_insert_failure_mid_transaction_rolls_back_delete_and_sources_upsert`) is
    the one that actually proves the transaction rolls back: it fails INSIDE the transaction, after
    the DELETE has already executed.
    """
    source_url = "https://example.gov/freshness-test-reindex-embedder-failure"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    old_row_id = await _insert_test_row(conn, source_url, fetched_at=old_time)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, content FROM documents WHERE source_url = %s ORDER BY id",
            (source_url,),
        )
        before_rows = await cur.fetchall()
    assert [r["id"] for r in before_rows] == [old_row_id]

    chunks = [
        {
            "heading": "New Heading",
            "level": 2,
            "parent": None,
            "breadcrumb": "New Heading",
            "text": "New Heading\n\nSome new content.",
        }
    ]

    with pytest.raises(RuntimeError, match="simulated embedder failure"):
        await reindex_source(
            conn,
            _RaisingEmbedder(),
            source_url=source_url,
            resolved_url=None,
            page_last_updated=None,
            rule_effective_date=None,
            body="New Heading\n\nSome new content.",
            chunks=chunks,
            now=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        )

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, content FROM documents WHERE source_url = %s ORDER BY id",
            (source_url,),
        )
        after_rows = await cur.fetchall()
    assert after_rows == before_rows

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT change_count FROM sources WHERE source_url = %s", (source_url,))
        source_row = await cur.fetchone()
    assert source_row["change_count"] == 0, "a failed reindex must never be recorded as a change"

    await _delete_test_rows(conn, source_url)


class _WrongDimensionEmbedder:
    """Returns real vectors, but of the wrong dimension (3, not 768) -- `embed()` itself succeeds,
    so `_embed_and_store` reaches its transaction, runs the `sources` upsert, runs the DELETE, and
    only THEN fails, when Postgres rejects the mismatched vector on INSERT
    (`psycopg.errors.DataException`, verified directly against a live pgvector column: "expected
    768 dimensions, not 3"). This is what makes the test below a real proof of ROLLBACK, unlike
    `test_reindex_source_embedder_failure_leaves_existing_chunks_intact` above.
    """

    async def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


async def test_reindex_source_insert_failure_mid_transaction_rolls_back_delete_and_sources_upsert(
    conn,
):
    """DoD 1(a), the STRONGER proof: the failure happens INSIDE the transaction, after the DELETE
    has already run against `documents` and after the `sources` upsert has already run -- so a
    green result here actually exercises the ROLLBACK, not merely "nothing happened before the
    transaction opened" (see the weaker test above). Asserts BOTH halves survive unchanged: the
    pre-existing `documents` rows (by id and content) and the pre-existing `sources` row's fields
    (fetched_at/last_verified_at/change_count) that the same transaction's upsert would otherwise
    have overwritten.
    """
    source_url = "https://example.gov/freshness-test-reindex-wrong-dimension"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    old_body = "old content"
    old_row_id = await _insert_test_row(
        conn,
        source_url,
        fetched_at=old_time,
        page_last_updated=date(2026, 1, 1),
        last_indexed_body=old_body,
    )

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, content FROM documents WHERE source_url = %s ORDER BY id",
            (source_url,),
        )
        before_rows = await cur.fetchall()
    assert [r["id"] for r in before_rows] == [old_row_id]

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fetched_at, last_verified_at, page_last_updated, change_count, "
            "last_indexed_body FROM sources WHERE source_url = %s",
            (source_url,),
        )
        before_source = await cur.fetchone()

    chunks = [
        {
            "heading": "New Heading",
            "level": 2,
            "parent": None,
            "breadcrumb": "New Heading",
            "text": "New Heading\n\nSome new content.",
        }
    ]

    with pytest.raises(psycopg.errors.DataException, match="768 dimensions"):
        await reindex_source(
            conn,
            _WrongDimensionEmbedder(),
            source_url=source_url,
            resolved_url=None,
            page_last_updated=date(2026, 9, 1),
            rule_effective_date=None,
            body="New Heading\n\nSome new content.",
            chunks=chunks,
            now=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        )
    await conn.rollback()

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, content FROM documents WHERE source_url = %s ORDER BY id",
            (source_url,),
        )
        after_rows = await cur.fetchall()
    assert after_rows == before_rows, "the DELETE that ran before the failed INSERT must roll back"

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fetched_at, last_verified_at, page_last_updated, change_count, "
            "last_indexed_body FROM sources WHERE source_url = %s",
            (source_url,),
        )
        after_source = await cur.fetchone()
    assert (
        after_source == before_source
    ), "the sources upsert that ran before the failed INSERT must roll back too"
    assert (
        after_source["last_indexed_body"] == old_body
    ), "a failed reindex must never leave the NEW body committed, rolled-back diff baseline or not"

    await _delete_test_rows(conn, source_url)


async def test_embed_and_store_first_index_writes_last_indexed_body_and_rule_effective_date(conn):
    """DoD 4's other half: `app/ingest.py::_embed_and_store` (the function BOTH a first-time
    `python -m app.ingest` and app/recrawl.py::reindex_source's meaningful-change path funnel
    through) must write `sources.last_indexed_body`/`rule_effective_date` on a genuinely first-time
    index -- a brand-new source_url with no prior `sources` row at all -- not just on a re-index of
    an already-known source (already proven by
    test_reindex_source_replaces_rows_and_moves_fetched_at_and_page_last_updated above).
    """
    source_url = "https://example.gov/freshness-test-first-index-body"
    body = "# Test Page\n\n## Section\n\nSome first-time content.\n"
    rule_effective_date = date(2026, 9, 15)
    chunks = [
        {
            "heading": "Section",
            "level": 2,
            "parent": None,
            "breadcrumb": "Test Page > Section",
            "text": "Test Page > Section\n\nSome first-time content.",
        }
    ]

    await _embed_and_store(
        conn,
        StubEmbedder(dim=768),
        source_url=source_url,
        resolved_url=None,
        page_last_updated=date(2026, 1, 1),
        body=body,
        rule_effective_date=rule_effective_date,
        chunks=chunks,
    )

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT last_indexed_body, rule_effective_date FROM sources WHERE source_url = %s",
            (source_url,),
        )
        source_row = await cur.fetchone()
    assert source_row["last_indexed_body"] == body
    assert source_row["rule_effective_date"] == rule_effective_date

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT rule_effective_date FROM documents WHERE source_url = %s", (source_url,)
        )
        doc_row = await cur.fetchone()
    assert doc_row["rule_effective_date"] == rule_effective_date

    await _delete_test_rows(conn, source_url)


async def test_no_foreign_key_touching_documents_is_on_delete_cascade(conn):
    """DoD 1(b), SCHEMA half: query pg_constraint directly for every foreign key that references
    `documents` or is defined ON `documents`, and assert none of them is ON DELETE CASCADE
    (confdeltype = 'c'). CASCADE would create exactly the path this phase exists to rule out:
    deleting a `sources` row silently deleting every chunk that cites it, with nothing re-inserted
    to replace them.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT conname, confdeltype
            FROM pg_constraint
            WHERE contype = 'f'
              AND (conrelid = 'documents'::regclass OR confrelid = 'documents'::regclass)
            """
        )
        rows = await cur.fetchall()

    assert rows, "expected at least one foreign key referencing or defined on `documents`"
    cascades = [r for r in rows if r["confdeltype"] == "c"]
    assert cascades == [], f"found ON DELETE CASCADE foreign key(s) touching documents: {cascades}"


async def test_deleting_a_sources_row_with_chunks_raises_foreign_key_violation(conn):
    """DoD 1(b), BEHAVIORAL half: deleting a `sources` row that still has chunks referencing it
    must raise a real ForeignKeyViolation, not silently cascade-delete those chunks.
    """
    source_url = "https://example.gov/freshness-test-fk-restrict"
    await _insert_test_row(conn, source_url, fetched_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM sources WHERE source_url = %s", (source_url,))
    await conn.rollback()

    await _delete_test_rows(conn, source_url)


_DESTRUCTIVE_DOCUMENTS_RE = re.compile(
    r"\b(?:DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?(?:\s+ONLY)?|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)"
    r"\s+(?:\w+\.)?documents\b",
    re.IGNORECASE | re.DOTALL,
)


def _find_dir_containing_app_package(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "app" / "ingest.py").is_file():
            return candidate
    raise AssertionError(
        "could not locate services/orchestrator/app by searching upward from this test file"
    )


def test_destructive_documents_statements_only_live_in_embed_and_store():
    """DoD 1(c), SOURCE GUARD: scan every .py file under services/orchestrator/app/ for any
    destructive statement against the `documents` table -- DELETE FROM documents, TRUNCATE
    documents, DROP TABLE documents, case-insensitive, schema-qualified or not, with arbitrary
    whitespace/newlines between tokens -- and assert every occurrence lives inside
    app/ingest.py::_embed_and_store, the single call site that also performs the replacement INSERT
    in the same transaction. Asserted BROADLY over the whole class of statements, and over every
    file under app/ (via rglob, evaluated at test-run time), not just the one known statement in
    ingest.py today -- this must fail the moment someone adds a DELETE/TRUNCATE/DROP against
    `documents` anywhere else, including a new file that does not exist yet.
    """
    app_root = _find_dir_containing_app_package(Path(__file__).resolve()) / "app"
    ingest_path = app_root / "ingest.py"

    ingest_source = ingest_path.read_text(encoding="utf-8")
    tree = ast.parse(ingest_source, filename=str(ingest_path))
    embed_and_store_range: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_embed_and_store":
            embed_and_store_range = (node.lineno, node.end_lineno)
            break
    assert embed_and_store_range is not None, "could not locate _embed_and_store in app/ingest.py"
    start_line, end_line = embed_and_store_range

    violations: list[str] = []
    total_matches = 0
    for path in sorted(app_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _DESTRUCTIVE_DOCUMENTS_RE.finditer(text):
            total_matches += 1
            line_no = text.count("\n", 0, match.start()) + 1
            if path != ingest_path or not (start_line <= line_no <= end_line):
                violations.append(f"{path}:{line_no}: {match.group(0)!r}")

    assert total_matches > 0, "the destructive-statement regex matched nothing at all in app/"
    assert violations == [], (
        "found a destructive statement against `documents` outside "
        f"app/ingest.py::_embed_and_store: {violations}"
    )


async def test_record_source_failure_never_advances_verification_or_success_clocks(
    conn,
):
    """DoD 2: record_source_failure must NEVER write last_verified_at/last_success_at/fetched_at/
    last_changed_at/change_count -- a failing source's verification clock must not silently
    advance. Captures every one of those columns before the call and asserts them byte-identical
    after; asserts consecutive_failures/last_error/last_http_status/status DID move.
    """
    source_url = "https://example.gov/freshness-test-record-failure-clocks"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    await _insert_test_row(conn, source_url, fetched_at=old_time)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fetched_at, last_verified_at, last_success_at, last_changed_at, "
            "change_count FROM sources WHERE source_url = %s",
            (source_url,),
        )
        before = await cur.fetchone()

    await record_source_failure(
        conn,
        source_url,
        error="ConnectTimeout: timed out",
        http_status=None,
        status="fetch_failed",
    )

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fetched_at, last_verified_at, last_success_at, last_changed_at, "
            "change_count, consecutive_failures, last_error, last_http_status, status "
            "FROM sources WHERE source_url = %s",
            (source_url,),
        )
        after = await cur.fetchone()

    assert after["fetched_at"] == before["fetched_at"]
    assert after["last_verified_at"] == before["last_verified_at"]
    assert after["last_success_at"] == before["last_success_at"]
    assert after["last_changed_at"] == before["last_changed_at"]
    assert after["change_count"] == before["change_count"]

    assert after["consecutive_failures"] == 1
    assert after["last_error"] == "ConnectTimeout: timed out"
    assert after["last_http_status"] is None
    assert after["status"] == "fetch_failed"

    await _delete_test_rows(conn, source_url)


async def test_record_source_failure_robots_disallowed_does_not_increment_failures(
    conn,
):
    """The other branch of record_source_failure: status="robots_disallowed" moves last_error/
    status but deliberately does NOT increment consecutive_failures (a robots disallow is a
    permanent curation condition, not a fetch failure to count)."""
    source_url = "https://example.gov/freshness-test-record-failure-robots"
    await _insert_test_row(conn, source_url, fetched_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    await record_source_failure(
        conn,
        source_url,
        error="robots_disallowed",
        http_status=None,
        status="robots_disallowed",
    )

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT consecutive_failures, last_error, last_http_status, status "
            "FROM sources WHERE source_url = %s",
            (source_url,),
        )
        row = await cur.fetchone()

    assert row["consecutive_failures"] == 0
    assert row["last_error"] == "robots_disallowed"
    assert row["last_http_status"] is None
    assert row["status"] == "robots_disallowed"

    await _delete_test_rows(conn, source_url)


# =================================================================================================
# PHASE 7 pure: app/guardrails/freshness.py::source_health_state (no DB, no clock of its own)
# =================================================================================================

_HEALTH_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("consecutive_failures", "status", "last_success_at", "expected"),
    [
        (0, "ok", _HEALTH_NOW, "ok"),
        (2, "ok", _HEALTH_NOW, "ok"),  # boundary: 2 failures is still ok
        (3, "ok", _HEALTH_NOW, "broken"),  # boundary: 3 failures is broken
        (5, "ok", _HEALTH_NOW, "broken"),  # well past the threshold
        (0, "robots_disallowed", _HEALTH_NOW, "broken"),  # permanent, immediate
        (
            0,
            "ok",
            _HEALTH_NOW - timedelta(days=7),
            "ok",
        ),  # boundary: exactly 7 days is still ok
        (
            0,
            "ok",
            _HEALTH_NOW - timedelta(days=7, seconds=1),
            "broken",
        ),  # just past 7 days
        (0, "ok", _HEALTH_NOW - timedelta(days=30), "broken"),
        (0, "ok", None, "broken"),  # never once succeeded
    ],
)
def test_source_health_state_each_disjunct_and_boundary(
    consecutive_failures, status, last_success_at, expected
):
    assert (
        source_health_state(
            consecutive_failures=consecutive_failures,
            status=status,
            last_success_at=last_success_at,
            now=_HEALTH_NOW,
            broken_after_failures=3,
            broken_after_no_success_days=7,
        )
        == expected
    )


# =================================================================================================
# FRESHNESS GUARDRAIL: app/guardrails/freshness.py (no langgraph, no DB)
# =================================================================================================


def _make_chunk(**overrides) -> RetrievedChunk:
    defaults = dict(
        id=1,
        content="chunk text",
        source_url="https://example.gov/a",
        resolved_url=None,
        section_heading="Heading",
        heading_level=2,
        page_last_updated=None,
        rule_effective_date=None,
        fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
        last_verified_at=datetime(2026, 8, 1, tzinfo=UTC),
        distance=0.1,
        rrf_score=0.5,
        semantic_rank=1,
        keyword_rank=None,
    )
    defaults.update(overrides)
    return RetrievedChunk(**defaults)


def test_build_freshness_top_ranked_uncited_dated_source_produces_a_notice_with_link():
    """The single retrieved chunk sits at position 1 (`chunks[0]`) -- condition (a), "top_ranked"
    -- with an EMPTY `cited_indices`, so this is a top-ranked-but-uncited case, not a cited one.
    """
    chunk = _make_chunk(
        source_url="https://studyinthestates.dhs.gov/quick-facts",
        rule_effective_date=date(2026, 9, 15),
    )

    freshness = build_freshness([chunk], today=date(2026, 9, 5), cited_indices=set())

    assert len(freshness.notices) == 1
    notice = freshness.notices[0]
    assert notice.source_url == "https://studyinthestates.dhs.gov/quick-facts"
    assert notice.rule_effective_date == date(2026, 9, 15)
    assert notice.in_effect is False
    assert notice.reason == "top_ranked"

    text = freshness_notice_text(freshness.notices)
    assert text is not None
    assert "September 15, 2026" in text
    assert "https://studyinthestates.dhs.gov/quick-facts" in text


def test_build_freshness_cited_but_not_top_ranked_dated_source_produces_a_notice():
    """Condition (b), "cited": the dated source's chunk sits at position 2, not position 1, but
    its position is in `cited_indices` -- the generated answer's own bracket citation. This is the
    live Phase 5 defect's fix: retrieval merely returning a dated chunk somewhere in the list is not
    enough (see the non-qualifying test below); it has to be ranked first OR actually cited.
    """
    chunks = [
        _make_chunk(source_url="https://example.gov/unrelated", rule_effective_date=None),
        _make_chunk(
            source_url="https://studyinthestates.dhs.gov/quick-facts",
            rule_effective_date=date(2026, 9, 15),
        ),
    ]

    freshness = build_freshness(chunks, today=date(2026, 9, 5), cited_indices={2})

    assert len(freshness.notices) == 1
    notice = freshness.notices[0]
    assert notice.source_url == "https://studyinthestates.dhs.gov/quick-facts"
    assert notice.reason == "cited"

    text = freshness_notice_text(freshness.notices)
    assert text is not None
    assert "September 15, 2026" in text


def test_build_freshness_dated_source_neither_top_ranked_nor_cited_produces_no_notice():
    """The exact spurious case the Phase 5 defect report describes: a dated source is retrieved
    (position 2, not top-ranked) but the generated answer never cited it. No notice, no appended
    text -- but its `rule_effective_date` is still visible in `Freshness.sources`, so nothing about
    that source's own freshness bookkeeping is lost, only the unwarranted prose warning is.
    """
    dated_url = "https://studyinthestates.dhs.gov/quick-facts"
    chunks = [
        _make_chunk(source_url="https://example.gov/unrelated", rule_effective_date=None),
        _make_chunk(source_url=dated_url, rule_effective_date=date(2026, 9, 15)),
    ]

    freshness = build_freshness(chunks, today=date(2026, 9, 5), cited_indices=set())

    assert freshness.notices == []
    assert freshness_notice_text(freshness.notices) is None

    dated_source = next(s for s in freshness.sources if s.source_url == dated_url)
    assert dated_source.rule_effective_date == date(2026, 9, 15)


def test_freshness_notice_text_collapses_two_sources_sharing_one_date_into_one_sentence():
    """The live Phase 5 case: both fixed_admission snapshots carry rule_effective_date=2026-09-15,
    and a question retrieving both must not read the same sentence twice with two different links.
    `Freshness.notices` itself stays one-per-source; only the rendered prose collapses. url_a
    qualifies as top-ranked (position 1); url_b qualifies as cited (position 2, cited_indices={2})
    -- both qualifying routes feed the same collapsing logic.
    """
    url_a = "https://studyinthestates.dhs.gov/final-rule-establishing-a-fixed-time-period-of-admission-and-an-extension-of-stay-procedure-faq"
    url_b = "https://studyinthestates.dhs.gov/final-rule-establishing-a-fixed-time-period-of-admission-and-an-extension-of-stay-quick-facts"
    chunks = [
        _make_chunk(source_url=url_a, rule_effective_date=date(2026, 9, 15)),
        _make_chunk(source_url=url_b, rule_effective_date=date(2026, 9, 15)),
    ]

    freshness = build_freshness(chunks, today=date(2026, 9, 5), cited_indices={2})

    # The structured field stays granular: one notice per source.
    assert len(freshness.notices) == 2
    assert {n.source_url for n in freshness.notices} == {url_a, url_b}
    by_url = {n.source_url: n for n in freshness.notices}
    assert by_url[url_a].reason == "top_ranked"
    assert by_url[url_b].reason == "cited"

    text = freshness_notice_text(freshness.notices)
    assert text is not None
    assert (
        text.count("September 15, 2026") == 1
    ), f"expected exactly one sentence for the shared date, got: {text!r}"
    assert url_a in text
    assert url_b in text


def test_freshness_notice_text_keeps_separate_sentences_for_different_dates():
    chunks = [
        _make_chunk(source_url="https://example.gov/a", rule_effective_date=date(2026, 9, 15)),
        _make_chunk(source_url="https://example.gov/b", rule_effective_date=date(2027, 1, 1)),
    ]

    freshness = build_freshness(chunks, today=date(2026, 9, 5), cited_indices={2})
    text = freshness_notice_text(freshness.notices)

    assert text is not None
    assert "September 15, 2026" in text
    assert "January 1, 2027" in text
    assert text.count("https://example.gov/a") == 1
    assert text.count("https://example.gov/b") == 1


def test_build_freshness_without_rule_effective_date_produces_no_notice_and_no_text():
    chunk = _make_chunk(rule_effective_date=None)

    freshness = build_freshness([chunk], today=date(2026, 9, 5), cited_indices=set())

    assert freshness.notices == []
    assert freshness_notice_text(freshness.notices) is None


# =================================================================================================
# PIPELINE-LEVEL freshness (no langgraph, no DB): driven with a monkeypatched hybrid_search and a
# fake LLM whose citations are fully controlled by the test, so DoD 3 is proven by logic the test
# itself dictates -- never by which chunks a real corpus's retrieval, or a content-blind stub
# embedder's hash, happens to rank where.
#
# The previous version of this section drove the real hybrid_search against a real ingested
# corpus and asserted that a specific source ("Admit Until Date") landed among the top
# RETRIEVAL_TOP_K results, on the theory that the keyword arm made that reliable. It measured true
# on the 17-chunk CI fixture corpus (the keyword arm's top ranks were ALL the dated source there)
# and false on the real 221-chunk corpus (plainto_tsquery's OR-ed terms there rank the dated source
# only 3rd-5th, behind unrelated STEM OPT/OPT chunks, so the stub embedder's content-blind semantic
# arm pushed it out of the fused top 5 entirely) -- see docs/reports/phase-5.md's "Correction: the
# DoD 3 test was fragile" for the full measurement. That made the test's outcome a function of
# corpus composition, not of the freshness-notice logic it was meant to prove. Monkeypatching
# hybrid_search removes that dependency entirely: `chunks` below IS the retrieval result, in the
# exact order and with the exact rule_effective_date values each case needs, and a FixedAnswerLLM
# makes "cited" vs. "not cited" a fact the test sets rather than one a real (or stub) generator
# happens to produce.
# =================================================================================================


@pytest.fixture
def embedder():
    return StubEmbedder(dim=768)


@pytest.fixture
def llm():
    return StubLLM()


@pytest.fixture
def settings():
    return Settings(LLM_PROVIDER="stub", EMBED_PROVIDER="stub", NO_ANSWER_MAX_DISTANCE=2.0)


class ExplodingLLM(LLM):
    async def generate(self, system: str, user: str) -> str:
        raise AssertionError("LLM.generate must not be called")


class ExplodingPool:
    def __getattr__(self, name):
        raise AssertionError(f"pool.{name} must not be touched on the CLARIFY path")


class ExplodingEmbedder:
    async def embed(self, texts):
        raise AssertionError("embedder.embed must not be called on the CLARIFY path")


@pytest.fixture
async def pool():
    settings = get_settings()
    database_url_ = os.environ.get("DATABASE_URL", settings.DATABASE_URL)
    p = make_pool(database_url_)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


class FixedAnswerLLM(LLM):
    """A fake LLM that always returns a fixed string, so a test can dictate exactly which bracket
    indices are "cited" and which are not -- the same pattern
    tests/test_guardrails.py::FixedAnswerLLM uses for the same reason.
    """

    def __init__(self, text: str):
        self._text = text

    async def generate(self, system: str, user: str) -> str:
        return self._text


def _patch_hybrid_search(monkeypatch: pytest.MonkeyPatch, chunks: list[RetrievedChunk]) -> None:
    """Replace app.pipeline's own reference to hybrid_search with a fake that ignores every
    argument (pool, embedding, question, k, rrf_k, candidate_pool) and always returns `chunks`, in
    the exact order given. This is what makes the tests below independent of any corpus, any
    embedder, and ts_rank_cd's behavior on any particular query text: retrieval order and content
    are dictated by the test, not discovered from a database.
    """

    async def _fake_hybrid_search(*args, **kwargs):
        del args, kwargs
        return chunks

    monkeypatch.setattr("app.pipeline.hybrid_search", _fake_hybrid_search)


# A question with no advice-seeking phrasing (so Layer 1 of app.guardrails.classifier never fires)
# and enough content words that app.guardrails.clarifier never fires either -- both guards run
# before retrieval, using only the question text (see app/pipeline.py), so the same question text
# is reused, unchanged, across every case below; only `chunks` and the fake LLM's answer differ.
_QUESTION = "What does the fixed period of admission rule say for F-1 students?"


async def test_pipeline_appends_notice_reason_top_ranked_for_an_uncited_dated_top_chunk(
    monkeypatch, embedder, settings
):
    """DoD 3's live motivating case (docs/reports/phase-5.md: "How long do I have to leave the
    United States after my OPT ends?"): the dated source ranks first, but the generated answer
    cites a DIFFERENT chunk for the still-current rule and never cites the dated one.
    build_freshness's "top_ranked" condition exists exactly so the notice still fires here.
    """
    dated_url = "https://example.gov/dated-top-ranked"
    other_url = "https://example.gov/other-cited-instead"
    chunks = [
        _make_chunk(id=1, source_url=dated_url, rule_effective_date=date(2026, 9, 15)),
        _make_chunk(id=2, source_url=other_url, rule_effective_date=None),
    ]
    _patch_hybrid_search(monkeypatch, chunks)
    fake_llm = FixedAnswerLLM("The current rule is stated here [2].")

    response = await answer_question(
        _QUESTION,
        pool=ExplodingPool(),
        embedder=embedder,
        llm=fake_llm,
        settings=settings,
    )

    assert response.response_type == ResponseType.ANSWER.value
    assert response.freshness is not None
    assert response.freshness.as_of == datetime.now(UTC).date()

    notices = response.freshness.notices
    assert len(notices) == 1, notices
    assert notices[0].source_url == dated_url
    assert notices[0].reason == "top_ranked"

    assert "September 15, 2026" in response.answer
    assert dated_url in response.answer


async def test_pipeline_appends_notice_reason_cited_for_a_non_top_dated_chunk_the_answer_cites(
    monkeypatch, embedder, settings
):
    """The other qualifying condition: the dated source is NOT ranked first, but the generated
    answer actually cited it (its 1-based position is in the answer's cited_indices)."""
    other_url = "https://example.gov/other-top-ranked"
    dated_url = "https://example.gov/dated-cited"
    chunks = [
        _make_chunk(id=1, source_url=other_url, rule_effective_date=None),
        _make_chunk(id=2, source_url=dated_url, rule_effective_date=date(2026, 9, 15)),
    ]
    _patch_hybrid_search(monkeypatch, chunks)
    fake_llm = FixedAnswerLLM("As the second source states [2], the rule changes soon.")

    response = await answer_question(
        _QUESTION,
        pool=ExplodingPool(),
        embedder=embedder,
        llm=fake_llm,
        settings=settings,
    )

    assert response.response_type == ResponseType.ANSWER.value
    assert response.freshness is not None

    notices = response.freshness.notices
    assert len(notices) == 1, notices
    assert notices[0].source_url == dated_url
    assert notices[0].reason == "cited"

    assert "September 15, 2026" in response.answer
    assert dated_url in response.answer


async def test_pipeline_appends_no_notice_for_a_dated_chunk_neither_top_ranked_nor_cited(
    monkeypatch, embedder, settings
):
    """The exact spurious case build_freshness's gating exists to prevent: a dated source retrieved
    incidentally (not top-ranked, never cited) must add NO text to the rendered answer, but its
    rule_effective_date still has to survive in the structured Freshness.sources bookkeeping.
    """
    other_url = "https://example.gov/other-only-cited"
    dated_url = "https://example.gov/dated-neither-top-nor-cited"
    chunks = [
        _make_chunk(id=1, source_url=other_url, rule_effective_date=None),
        _make_chunk(id=2, source_url=dated_url, rule_effective_date=date(2026, 9, 15)),
    ]
    _patch_hybrid_search(monkeypatch, chunks)
    fixed_text = "The rule is stated here [1]."
    fake_llm = FixedAnswerLLM(fixed_text)

    response = await answer_question(
        _QUESTION,
        pool=ExplodingPool(),
        embedder=embedder,
        llm=fake_llm,
        settings=settings,
    )

    assert response.response_type == ResponseType.ANSWER.value
    assert response.freshness is not None
    assert response.freshness.notices == []
    assert response.answer == fixed_text, (
        f"a dated source that is neither top-ranked nor cited must add no text to the answer, got "
        f"{response.answer!r}"
    )

    dated_source = next(s for s in response.freshness.sources if s.source_url == dated_url)
    assert dated_source.rule_effective_date == date(2026, 9, 15)


async def test_pipeline_appends_no_notice_when_no_retrieved_chunk_carries_a_dated_rule(
    monkeypatch, embedder, settings
):
    only_url = "https://example.gov/no-dated-rule-anywhere"
    chunks = [_make_chunk(id=1, source_url=only_url, rule_effective_date=None)]
    _patch_hybrid_search(monkeypatch, chunks)
    fixed_text = "The rule is stated here [1]."
    fake_llm = FixedAnswerLLM(fixed_text)

    response = await answer_question(
        _QUESTION,
        pool=ExplodingPool(),
        embedder=embedder,
        llm=fake_llm,
        settings=settings,
    )

    assert response.response_type == ResponseType.ANSWER.value
    assert response.freshness is not None
    assert response.freshness.notices == []
    assert response.answer == fixed_text


async def test_no_answer_and_clarify_responses_carry_no_freshness(pool, embedder):
    no_answer_settings = Settings(
        LLM_PROVIDER="stub", EMBED_PROVIDER="stub", NO_ANSWER_MAX_DISTANCE=-1.0
    )
    no_answer_response = await answer_question(
        "What is a Form I-515A and when is it issued?",
        pool=pool,
        embedder=embedder,
        llm=ExplodingLLM(),
        settings=no_answer_settings,
    )
    assert no_answer_response.response_type == ResponseType.NO_ANSWER.value
    assert no_answer_response.freshness is None

    clarify_settings = Settings(LLM_PROVIDER="stub", EMBED_PROVIDER="stub")
    clarify_response = await answer_question(
        "help",
        pool=ExplodingPool(),
        embedder=ExplodingEmbedder(),
        llm=ExplodingLLM(),
        settings=clarify_settings,
    )
    assert clarify_response.response_type == ResponseType.CLARIFY.value
    assert clarify_response.freshness is None


# full_corpus: calibrates nothing new here, just confirms the real embedder/live-shaped retrieval
# path still carries rule_effective_date through hybrid_search -- kept separate from the
# stub-driven pipeline test above so this file's non-full_corpus tests never need Ollama reachable.
@pytest.mark.full_corpus
async def test_hybrid_search_carries_rule_effective_date_through(pool):
    settings = get_settings()
    real_embedder = OllamaEmbedder(base_url=settings.OLLAMA_BASE_URL, model=settings.EMBED_MODEL)
    question = "What is the Admit Until Date and how is it determined?"
    [embedding] = await real_embedder.embed([question])
    results = await hybrid_search(
        pool,
        embedding,
        question,
        k=settings.RETRIEVAL_TOP_K,
        rrf_k=60,
        candidate_pool=20,
    )
    assert any(r.rule_effective_date == date(2026, 9, 15) for r in results), (
        f"expected at least one retrieved chunk to carry rule_effective_date=2026-09-15, got "
        f"{[(r.id, r.source_url, r.rule_effective_date) for r in results]}"
    )


def _require_ollama_reachable(base_url: str) -> None:
    """Skip cleanly (not a failure) if Ollama is not reachable at `base_url`. The test below needs
    a real embedding call and is already `full_corpus`-marked (excluded from CI, which never has
    Ollama); this additionally protects a dev machine, or a container used only for the write-guard
    verification below, that has no Ollama running at all.
    """
    try:
        httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=3.0)
    except httpx.HTTPError as exc:
        pytest.skip(f"Ollama not reachable at {base_url!r}: {exc}")


@pytest.mark.full_corpus
async def test_pipeline_freshness_notice_fires_via_top_ranked_on_the_live_corpus(pool):
    """The real-corpus counterpart to the monkeypatched pipeline tests above: same
    build_freshness gating logic, but fed real hybrid_search results from the committed real
    corpus with the real nomic-embed-text embedder, so this actually exercises retrieval ranking
    rather than a hand-built chunk list. Matches the live evidence recorded in
    docs/reports/phase-5.md's DoD 3 table: the departure-period question retrieves a dated
    fixed_admission source at rank 1 (qualifying via "top_ranked"), and the unrelated Form I-983
    question retrieves no dated source among its RETRIEVAL_TOP_K results at all.

    Read-only (hybrid_search is SELECT-only), so this needs no DB-writing guard. Generation is
    deliberately out of scope here -- "top_ranked" is a fact about retrieval order alone, and
    driving a real (or stub) generator on top of it would only add a second, unrelated source of
    flakiness to a test whose job is to prove retrieval + build_freshness, not the full answer.
    """
    settings = get_settings()
    _require_ollama_reachable(settings.OLLAMA_BASE_URL)
    real_embedder = OllamaEmbedder(base_url=settings.OLLAMA_BASE_URL, model=settings.EMBED_MODEL)

    async def notices_for(question: str) -> list:
        [embedding] = await real_embedder.embed([question])
        chunks = await hybrid_search(
            pool,
            embedding,
            question,
            k=settings.RETRIEVAL_TOP_K,
            rrf_k=60,
            candidate_pool=20,
        )
        freshness = build_freshness(chunks, today=datetime.now(UTC).date(), cited_indices=set())
        return freshness.notices

    departure_notices = await notices_for(
        "How long do I have to leave the United States after my OPT ends?"
    )
    assert departure_notices, "expected the departure-period question to fire a freshness notice"
    assert any(
        n.reason == "top_ranked" for n in departure_notices
    ), f"expected a top_ranked notice for the departure-period question, got {departure_notices}"

    i983_notices = await notices_for("What is the I-983 and who fills it out?")
    assert (
        i983_notices == []
    ), f"expected no freshness notice for the I-983 question, got {i983_notices}"


# =================================================================================================
# GRAPH: app/recrawl.py's LangGraph refresh graph (requires_langgraph, DB-backed against a scratch
# database -- see conftest for how DATABASE_URL/officehours_freshness is wired up locally).
# =================================================================================================


class KillError(BaseException):
    """Simulates an unrecoverable process kill (SIGKILL, OOM, host crash) -- NOT caught by
    app.recrawl's own `except Exception` clauses (in `_fetch_node` and `run_refresh`'s per-source
    loop), the same reason `KeyboardInterrupt`/`SystemExit`/`asyncio.CancelledError` subclass
    `BaseException` rather than `Exception` in the standard library: those `except Exception`
    clauses exist to isolate ORDINARY per-source failures (a bad HTTP response, a bug in one
    source's own processing) from stopping the whole run, not to make the whole run un-killable. A
    real kill signal is never something application code gets a chance to catch at all.
    """


def _write_test_snapshot(
    raw_dir: Path, *, source_url: str, topic: str, title: str, body: str
) -> Path:
    frontmatter = {
        "source_url": source_url,
        "title": title,
        "fetched_at": "2026-01-01",
        "page_last_updated": "2026-01-01",
        "topic": topic,
    }
    path = mint_snapshot_filename(topic, source_url, raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(render_snapshot(frontmatter, body), encoding="utf-8")
    return path


@requires_langgraph
@pytest.mark.freshness
async def test_graph_meaningful_change_reindexes_and_rewrites_the_snapshot(
    tmp_path, conn, database_url
):
    source_url = "https://example.gov/freshness-graph-meaningful"
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    old_body = "# Test Page\n\n## Section\n\nThe extension is 24 months.\n"
    snapshot_path = _write_test_snapshot(
        tmp_path,
        source_url=source_url,
        topic=topic,
        title="Test Page",
        body=old_body,
    )
    old_row_id = await _insert_test_row(
        conn,
        source_url,
        fetched_at=old_time,
        page_last_updated=date(2026, 1, 1),
        last_indexed_body=old_body,
    )

    async def fetcher(entry):
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown="# Test Page\n\n## Section\n\nThe extension is 36 months.\n",
        )

    report = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Test Page"}],
        raw_dir=tmp_path,
        run_id="test-run-meaningful",
        checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )

    assert len(report.results) == 1
    result = report.results[0]
    assert result.status == "meaningful"
    assert result.chunks_indexed > 0

    new_snapshot_text = snapshot_path.read_text(encoding="utf-8")
    assert "36 months" in new_snapshot_text
    assert "24 months" not in new_snapshot_text

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT id FROM documents WHERE source_url = %s", (source_url,))
        rows = await cur.fetchall()

    assert rows
    assert all(r["id"] != old_row_id for r in rows)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fetched_at, last_indexed_body FROM sources WHERE source_url = %s",
            (source_url,),
        )
        source_row = await cur.fetchone()
    assert source_row["fetched_at"] > old_time
    # The freshly fetched body -- never the old one, and never a chunk's own (breadcrumb-prefixed)
    # text -- is what the NEXT recrawl's diff must compare against (docs/adr/0014).
    assert (
        source_row["last_indexed_body"]
        == "# Test Page\n\n## Section\n\nThe extension is 36 months.\n"
    )

    await _delete_test_rows(conn, source_url)


@requires_langgraph
@pytest.mark.freshness
async def test_graph_meaningful_change_carries_verdict_evidence_through_source_result(
    tmp_path, conn, database_url
):
    """R1: `SourceResult` must carry the `ChangeVerdict` evidence (`added_count`, `removed_count`,
    `highlights`) computed inside `_diff_node`, not just `status`/`reason` -- on BOTH the fresh-run
    path and the already-terminal/resumed path (`_verdict_evidence` reading
    `snapshot.values["verdict"]`). Driven entirely through `run_refresh` with a fake fetcher, so
    this goes through real graph state rather than constructing a `SourceResult` by hand.
    """
    source_url = "https://example.gov/freshness-graph-verdict-evidence"
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    _write_test_snapshot(
        tmp_path,
        source_url=source_url,
        topic=topic,
        title="Test Page",
        body="# Test Page\n\n## Section\n\nThe extension is 24 months.\n",
    )
    await _insert_test_row(
        conn,
        source_url,
        fetched_at=old_time,
        page_last_updated=date(2026, 1, 1),
        last_indexed_body="# Test Page\n\n## Section\n\nThe extension is 24 months.\n",
    )

    async def fetcher(entry):
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown="# Test Page\n\n## Section\n\nThe extension is 36 months.\n",
        )

    run_id = "test-run-verdict-evidence"
    checkpoint_path = str(tmp_path / "checkpoint.sqlite")

    report = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Test Page"}],
        raw_dir=tmp_path,
        run_id=run_id,
        checkpoint_path=checkpoint_path,
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )

    assert len(report.results) == 1
    result = report.results[0]
    assert result.status == "meaningful"
    assert result.resumed is False
    assert result.added_count > 0
    assert result.removed_count > 0
    assert any("36 months" in line for line in result.added)
    assert any("24 months" in line for line in result.removed)
    assert "36 months" in result.highlights

    # Resuming the SAME already-terminal source (same run_id + checkpoint file) must report the
    # SAME evidence, read from snapshot.values["verdict"] rather than dropping it.
    resumed_report = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Test Page"}],
        raw_dir=tmp_path,
        run_id=run_id,
        checkpoint_path=checkpoint_path,
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )

    assert len(resumed_report.results) == 1
    resumed_result = resumed_report.results[0]
    assert resumed_result.resumed is True
    assert resumed_result.status == "meaningful"
    assert resumed_result.added_count == result.added_count
    assert resumed_result.removed_count == result.removed_count
    assert resumed_result.highlights == result.highlights
    assert resumed_result.added == result.added
    assert resumed_result.removed == result.removed

    await _delete_test_rows(conn, source_url)


def _write_golden_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


@requires_langgraph
@pytest.mark.freshness
async def test_graph_meaningful_change_reports_exact_golden_impact_indices(
    tmp_path, conn, database_url
):
    """B1, driven through the real run_refresh pipeline (not just the pure function): a meaningful
    change to a source cited by two golden rows -- one citing the manifest URL, one citing only the
    URL the fetch actually redirected to -- reports BOTH indices, and the run goes red for it
    (docs/adr/0009).
    """
    source_url = "https://example.gov/freshness-graph-golden-impact"
    resolved_url = "https://example.gov/freshness-graph-golden-impact-resolved"
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    _write_test_snapshot(
        tmp_path,
        source_url=source_url,
        topic=topic,
        title="Test Page",
        body="# Test Page\n\n## Section\n\nThe extension is 24 months.\n",
    )
    await _insert_test_row(
        conn,
        source_url,
        fetched_at=old_time,
        page_last_updated=date(2026, 1, 1),
        last_indexed_body="# Test Page\n\n## Section\n\nThe extension is 24 months.\n",
    )

    async def fetcher(entry):
        return FetchedPage(
            resolved_url=resolved_url,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown="# Test Page\n\n## Section\n\nThe extension is 36 months.\n",
        )

    golden_path = tmp_path / "golden-impact-test.jsonl"
    _write_golden_jsonl(
        golden_path,
        [
            {"question": "cites the manifest url", "source_urls": [source_url]},
            {
                "question": "cites something unrelated",
                "source_urls": ["https://example.gov/x"],
            },
            {"question": "cites the RESOLVED url only", "source_urls": [resolved_url]},
        ],
    )

    report = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Test Page"}],
        raw_dir=tmp_path,
        run_id="test-run-golden-impact",
        checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
        golden_path=golden_path,
    )

    assert len(report.results) == 1
    result = report.results[0]
    assert result.status == "meaningful"
    assert result.golden_impact is not None
    assert result.golden_impact.available is True
    # Row 0 cites the manifest url, row 2 cites only the resolved url the fetch landed on -- BOTH
    # must be caught, not just the manifest-url match.
    assert result.golden_impact.affected_indices == [0, 2]

    assert report.exit_code() == 1  # golden rows are affected -> red, per docs/adr/0009
    payload = report.to_dict()
    assert payload["red_reasons"]["golden_review_sources"] == [source_url]
    assert payload["red_reasons"]["broken_sources"] == []

    await _delete_test_rows(conn, source_url)


@requires_langgraph
@pytest.mark.freshness
async def test_graph_meaningful_change_to_an_uncited_source_reports_empty_impact_and_stays_green(
    tmp_path, conn, database_url
):
    """B1/B2 negative case, driven through the real run_refresh pipeline: a meaningful change to a
    source NO golden row cites reports an empty affected list -- not a crash, not a false positive
    -- and the run stays green (docs/adr/0009: nothing needs a human here).
    """
    source_url = "https://example.gov/freshness-graph-golden-uncited"
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    _write_test_snapshot(
        tmp_path,
        source_url=source_url,
        topic=topic,
        title="Test Page",
        body="# Test Page\n\n## Section\n\nThe extension is 24 months.\n",
    )
    await _insert_test_row(
        conn,
        source_url,
        fetched_at=old_time,
        page_last_updated=date(2026, 1, 1),
        last_indexed_body="# Test Page\n\n## Section\n\nThe extension is 24 months.\n",
    )

    async def fetcher(entry):
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown="# Test Page\n\n## Section\n\nThe extension is 36 months.\n",
        )

    golden_path = tmp_path / "golden-impact-negative.jsonl"
    _write_golden_jsonl(
        golden_path,
        [
            {
                "question": "cites something else entirely",
                "source_urls": ["https://example.gov/x"],
            }
        ],
    )

    report = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Test Page"}],
        raw_dir=tmp_path,
        run_id="test-run-golden-uncited",
        checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
        golden_path=golden_path,
    )

    assert len(report.results) == 1
    result = report.results[0]
    assert result.status == "meaningful"
    assert result.golden_impact is not None
    assert result.golden_impact.available is True
    assert result.golden_impact.affected_indices == []
    assert report.exit_code() == 0  # nothing needs a human -- see docs/adr/0009

    await _delete_test_rows(conn, source_url)


@requires_langgraph
@pytest.mark.freshness
async def test_graph_cosmetic_change_only_touches_last_verified(tmp_path, conn, database_url):
    source_url = "https://example.gov/freshness-graph-cosmetic"
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    old_body = "# Test Page\n\n## Section\n\nThe extension is 24 months.\n"

    snapshot_path = _write_test_snapshot(
        tmp_path, source_url=source_url, topic=topic, title="Test Page", body=old_body
    )
    old_snapshot_bytes = snapshot_path.read_bytes()
    old_row_id = await _insert_test_row(
        conn,
        source_url,
        fetched_at=old_time,
        page_last_updated=date(2026, 1, 1),
        last_indexed_body=old_body,
    )

    async def fetcher(entry):
        # Cosmetic: same three lines, reordered -- see classify_change's step 3.
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown="# Test Page\n\nThe extension is 24 months.\n\n## Section\n",
        )

    report = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Test Page"}],
        raw_dir=tmp_path,
        run_id="test-run-cosmetic",
        checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )

    assert len(report.results) == 1
    result = report.results[0]
    assert result.status == "cosmetic"
    assert result.chunks_indexed == 0

    assert snapshot_path.read_bytes() == old_snapshot_bytes

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT id FROM documents WHERE source_url = %s", (source_url,))
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["id"] == old_row_id

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fetched_at, last_verified_at, last_indexed_body FROM sources "
            "WHERE source_url = %s",
            (source_url,),
        )
        source_row = await cur.fetchone()
    assert source_row["fetched_at"] == old_time
    assert source_row["last_verified_at"] > old_time
    # A cosmetic verdict never re-indexes, so the body the NEXT recrawl diffs against must stay
    # exactly what it already was -- never the differently-ordered text this run actually fetched.
    assert source_row["last_indexed_body"] == old_body

    await _delete_test_rows(conn, source_url)


# =================================================================================================
# Stateless recrawl (docs/adr/0014-stateless-recrawl-diff.md): _diff_node reads its baseline from
# `sources.last_indexed_body`, never from a snapshot file. This is the production scenario the
# whole change exists for -- a fresh checkout, or a stateless production host, has no
# data/sources/raw/ at all.
# =================================================================================================


@requires_langgraph
@pytest.mark.freshness
async def test_graph_stateless_run_classifies_unchanged_with_no_raw_dir_at_all(
    tmp_path, conn, database_url
):
    """The production scenario this whole change exists for: raw_dir is never created at all (a
    fresh checkout of data/sources/raw/, which is gitignored -- see .github/workflows/recrawl.yml),
    and the database already holds each source's previous body. Every source whose freshly
    fetched content matches that stored body must classify "unchanged", not "meaningful" -- an
    absent raw_dir must carry no information at all about whether content changed, because
    _diff_node never reads it for that purpose any more.
    """
    urls = [f"https://example.gov/stateless-run-{i}" for i in range(3)]
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    bodies = {
        url: f"# Stateless Page {i}\n\n## Section\n\nContent for stateless source {i}.\n"
        for i, url in enumerate(urls)
    }
    manifest = [
        {"url": u, "topic": topic, "title": f"Stateless Page {i}"} for i, u in enumerate(urls)
    ]

    for url in urls:
        await _insert_test_row(
            conn,
            url,
            fetched_at=old_time,
            page_last_updated=date(2026, 1, 1),
            last_indexed_body=bodies[url],
        )

    absent_raw_dir = tmp_path / "checkout-with-no-raw-snapshots"
    assert not absent_raw_dir.exists()

    async def fetcher(entry):
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown=bodies[entry["url"]],
        )

    report = await run_refresh(
        settings=get_settings(),
        manifest=manifest,
        raw_dir=absent_raw_dir,
        run_id="test-run-stateless",
        checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )

    assert len(report.results) == 3
    for result in report.results:
        assert result.status == "unchanged", (result.source_url, result.status, result.reason)
        assert result.reason == "identical_after_normalization"

    # The unchanged path never invokes _chunk_node, so nothing ever writes to raw_dir -- proving
    # this run genuinely never depended on, or touched, the filesystem for its diff.
    assert not absent_raw_dir.exists()

    await _delete_test_rows(conn, *urls)


@requires_langgraph
@pytest.mark.freshness
async def test_graph_meaningful_change_stores_new_body_and_a_later_stateless_run_reports_unchanged(
    tmp_path, conn, database_url
):
    """DoD 4 end to end: a meaningful change stores the new body on `sources` (proven directly in
    test_reindex_source_replaces_rows_and_moves_fetched_at_and_page_last_updated already), and a
    SECOND, independent recrawl run -- a different run_id/checkpoint (a fresh graph invocation, not
    a resume) and a raw_dir that is once again completely absent -- must read that stored body back
    and report "unchanged" against it, never "meaningful" again.
    """
    source_url = "https://example.gov/stateless-body-write-then-rerun"
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    old_body = "# Test Page\n\n## Section\n\nThe extension is 24 months.\n"
    new_body = "# Test Page\n\n## Section\n\nThe extension is 36 months.\n"

    await _insert_test_row(
        conn,
        source_url,
        fetched_at=old_time,
        page_last_updated=date(2026, 1, 1),
        last_indexed_body=old_body,
    )

    async def fetcher(entry):
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown=new_body,
        )

    first_raw_dir = tmp_path / "raw-first-run"  # never pre-populated
    report = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Test Page"}],
        raw_dir=first_raw_dir,
        run_id="test-run-body-write-1",
        checkpoint_path=str(tmp_path / "checkpoint-1.sqlite"),
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )
    assert report.results[0].status == "meaningful"

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT last_indexed_body FROM sources WHERE source_url = %s", (source_url,)
        )
        row = await cur.fetchone()
    assert row["last_indexed_body"] == new_body

    async def fetcher_same(entry):
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown=new_body,
        )

    second_raw_dir = tmp_path / "raw-second-run"  # absent again -- a fresh, stateless checkout
    report2 = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Test Page"}],
        raw_dir=second_raw_dir,
        run_id="test-run-body-write-2",  # different run_id/checkpoint -> a fresh graph invocation
        checkpoint_path=str(tmp_path / "checkpoint-2.sqlite"),
        fetcher=fetcher_same,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )
    assert report2.results[0].status == "unchanged"
    assert report2.results[0].reason == "identical_after_normalization"

    await _delete_test_rows(conn, source_url)


@requires_langgraph
@pytest.mark.freshness
async def test_graph_diff_raises_loudly_when_backfill_has_not_run_for_an_existing_source(
    tmp_path, conn, database_url
):
    """DoD 2's unsafe case: a source that already has chunks in `documents` (the signature of a
    real, previously-ingested source) but whose `sources.last_indexed_body` is still NULL --
    exactly the state every pre-existing row is in immediately after the schema migration in
    infra/sql/init.sql, before `python -m app.backfill_source_bodies` has been run against it. This
    must never be silently treated the same as a genuinely new source: doing so would reset
    fetched_at and re-embed content that has not actually changed, for every already-ingested
    source, the very first time the refresh job runs after the migration. It must fail loudly
    instead -- surfaced through the same per-source isolation every other failure already gets
    (run_refresh's own per-source try/except), never swallowed into a silent "meaningful" verdict.
    """
    source_url = "https://example.gov/stateless-unsafe-backfill-missing"
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    # Deliberately NOT passing last_indexed_body -- this reproduces the exact post-migration,
    # pre-backfill state: a `sources` row with real `documents` chunks and a NULL body.
    await _insert_test_row(
        conn, source_url, fetched_at=old_time, page_last_updated=date(2026, 1, 1)
    )

    async def fetcher(entry):
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown="# Test Page\n\n## Section\n\nSome content.\n",
        )

    report = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Test Page"}],
        raw_dir=tmp_path,
        run_id="test-run-unsafe-backfill",
        checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )

    # run_refresh isolates one source's failure from the others (its own per-source try/except) --
    # so this must not raise out of run_refresh itself, but the source must be reported failed,
    # loudly, with a reason naming the real cause, never silently as "meaningful".
    assert len(report.results) == 1
    result = report.results[0]
    assert result.status == "fetch_failed"
    assert "backfill" in (result.reason or "").lower()
    assert result.status != "meaningful"
    assert report.exit_code() == 1

    # fetched_at/last_verified_at must NOT have moved -- the whole point is that this source's
    # freshness history survives untouched until a human runs the backfill.
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fetched_at, last_verified_at FROM sources WHERE source_url = %s",
            (source_url,),
        )
        row = await cur.fetchone()
    assert row["fetched_at"] == old_time
    assert row["last_verified_at"] == old_time

    await _delete_test_rows(conn, source_url)


@requires_langgraph
@pytest.mark.freshness
async def test_graph_fetch_failure_isolation(tmp_path, conn, database_url):
    good_url = "https://example.gov/freshness-graph-good"
    bad_url = "https://example.gov/freshness-graph-bad"
    topic = "freshness_test"

    async def fetcher(entry):
        if entry["url"] == bad_url:
            raise RuntimeError("simulated network failure")
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown="# Good Page\n\n## Section\n\nSome content here.\n",
        )

    report = await run_refresh(
        settings=get_settings(),
        manifest=[
            {"url": good_url, "topic": topic, "title": "Good Page"},
            {"url": bad_url, "topic": topic, "title": "Bad Page"},
        ],
        raw_dir=tmp_path,
        run_id="test-run-failure-isolation",
        checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )

    by_url = {r.source_url: r for r in report.results}
    assert by_url[bad_url].status == "fetch_failed"
    assert by_url[bad_url].attempts == 3

    assert by_url[good_url].status == "meaningful"

    await _delete_test_rows(conn, good_url, bad_url)


@requires_langgraph
@pytest.mark.freshness
async def test_graph_resume_does_not_refetch_completed_sources(tmp_path, conn, database_url):
    urls = [f"https://example.gov/freshness-graph-resume-{i}" for i in range(3)]
    topic = "freshness_test"
    manifest = [{"url": u, "topic": topic, "title": f"Page {i}"} for i, u in enumerate(urls)]

    call_log: list[str] = []

    async def killing_fetcher(entry):
        call_log.append(entry["url"])
        if entry["url"] == urls[2]:
            raise KillError("simulated process kill")
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown=f"# Page\n\n## Section\n\nContent for {entry['url']}.\n",
        )

    checkpoint_path = str(tmp_path / "checkpoint.sqlite")
    run_id = "test-run-resume"

    with pytest.raises(KillError):
        await run_refresh(
            settings=get_settings(),
            manifest=manifest,
            raw_dir=tmp_path,
            run_id=run_id,
            checkpoint_path=checkpoint_path,
            fetcher=killing_fetcher,
            conn_factory=make_conn_factory(database_url),
            embedder=StubEmbedder(dim=768),
            max_attempts=3,
            backoff_seconds=0,
        )

    assert call_log == [urls[0], urls[1], urls[2]]
    calls_before_resume = len(call_log)

    async def working_fetcher(entry):
        call_log.append(entry["url"])
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown=f"# Page\n\n## Section\n\nContent for {entry['url']}.\n",
        )

    report = await run_refresh(
        settings=get_settings(),
        manifest=manifest,
        raw_dir=tmp_path,
        run_id=run_id,  # SAME run_id -> SAME thread_ids -> resumes from checkpoint
        checkpoint_path=checkpoint_path,  # SAME checkpoint file
        fetcher=working_fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )

    # (a) sources completed before the kill were NOT fetched again -- count fetcher calls.
    new_calls = call_log[calls_before_resume:]
    assert urls[0] not in new_calls
    assert urls[1] not in new_calls
    assert urls[2] in new_calls  # killed mid-fetch, never completed -- must be retried

    # (b) every source ends with a terminal status after the second run.
    assert len(report.results) == 3
    statuses = {r.source_url: r.status for r in report.results}
    for url in urls:
        assert statuses[url] in {"unchanged", "cosmetic", "meaningful", "fetch_failed"}
    assert statuses[urls[0]] == "meaningful"
    assert statuses[urls[1]] == "meaningful"
    assert statuses[urls[2]] == "meaningful"

    await _delete_test_rows(conn, *urls)


class _KillOnceEmbedder:
    """Wraps a real embedder; raises `KillError` (BaseException, not caught by run_refresh's own
    `except Exception` clauses) the FIRST time `embed()` is called for this run, then delegates to
    the wrapped embedder on every subsequent call. `embed()` is called from inside
    `reindex_source` -- the last step of the meaningful-change path (fetch -> diff -> chunk ->
    embed(-node) -> reindex) -- so by the time this raises, fetch/diff/chunk have already run and
    LangGraph has already checkpointed their state.
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self.calls == 1:
            raise KillError("simulated process kill during reindex")
        return await self._inner.embed(texts)


@requires_langgraph
@pytest.mark.freshness
async def test_graph_resume_continues_mid_source_without_refetching(tmp_path, conn, database_url):
    """The other half of "a run that dies partway through resumes rather than restarting" from
    test_graph_resume_does_not_refetch_completed_sources above: that test proves the driver SKIPS
    sources that already reached a terminal status between runs. This proves the graph resumes
    MID-SOURCE -- the checkpoint buys more than just "don't redo whole sources already finished."

    A single source's own graph run is killed after fetch/diff/chunk have completed, INSIDE the
    reindex step (via the embedder, not the fetcher) -- confirmed against the real langgraph
    checkpointer with a standalone probe before writing this test: the first `run_refresh` call
    propagates `KillError` (as `test_graph_resume_does_not_refetch_completed_sources` above already
    establishes KillError does, being a BaseException), and a second call with the SAME run_id and
    checkpoint file and a working embedder resumes from the checkpointed state rather than
    restarting the graph from START.
    """
    url = "https://example.gov/freshness-graph-resume-mid-source"
    topic = "freshness_test"
    manifest = [{"url": url, "topic": topic, "title": "Resume Mid-Source Page"}]

    call_log: list[str] = []

    async def fetcher(entry):
        call_log.append(entry["url"])
        return FetchedPage(
            resolved_url=None,
            title=entry.get("title"),
            page_last_updated=date(2026, 2, 1),
            body_markdown="# Resume Mid-Source Page\n\n## Section\n\nSome content here.\n",
        )

    checkpoint_path = str(tmp_path / "checkpoint.sqlite")
    run_id = "test-run-resume-mid-source"
    embedder = _KillOnceEmbedder(StubEmbedder(dim=768))

    with pytest.raises(KillError):
        await run_refresh(
            settings=get_settings(),
            manifest=manifest,
            raw_dir=tmp_path,
            run_id=run_id,
            checkpoint_path=checkpoint_path,
            fetcher=fetcher,
            conn_factory=make_conn_factory(database_url),
            embedder=embedder,
            max_attempts=3,
            backoff_seconds=0,
        )

    assert call_log == [url]  # fetch completed once, before the kill

    report = await run_refresh(
        settings=get_settings(),
        manifest=manifest,
        raw_dir=tmp_path,
        run_id=run_id,  # SAME run_id -> SAME thread_id -> resumes from checkpoint
        checkpoint_path=checkpoint_path,  # SAME checkpoint file
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=embedder,  # SAME embedder: .calls is now 1, so its second call succeeds
        max_attempts=3,
        backoff_seconds=0,
    )

    # (a) the fetcher was NOT called again -- the graph resumed at the node it died on (reindex)
    # rather than restarting from START.
    assert call_log == [url]

    # (b) the source ends meaningful, with its rows actually written.
    assert len(report.results) == 1
    result = report.results[0]
    assert result.status == "meaningful"
    assert result.chunks_indexed > 0

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT id FROM documents WHERE source_url = %s", (url,))
        rows = await cur.fetchall()
    assert rows

    await _delete_test_rows(conn, url)


# =================================================================================================
# PHASE 7 graph: robots_disallowed vs. a transient fetch error, and 404 vs. timeout. Each source
# is pre-seeded via _insert_test_row (a real prior successful crawl) so record_source_failure has
# a `sources` row to update -- the real production flow always ingests a source successfully at
# least once (via `python -m app.ingest`) before the scheduled recrawl job's failure path can ever
# run against it.
# =================================================================================================


@requires_langgraph
@pytest.mark.freshness
async def test_graph_robots_disallowed_stops_at_one_attempt_and_does_not_count_as_a_failure(
    tmp_path, conn, database_url
):
    """DoD 3: a fetcher signaling a robots.txt disallow (returns None) must stop at attempts == 1
    (never retried, unlike a transient error), classify status == "robots_disallowed", and record
    that in `sources` WITHOUT incrementing consecutive_failures.
    """
    source_url = "https://example.gov/freshness-graph-robots-disallowed"
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    await _insert_test_row(conn, source_url, fetched_at=old_time)

    async def robots_fetcher(entry):
        del entry
        return None

    report = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Robots Page"}],
        raw_dir=tmp_path,
        run_id="test-run-robots-disallowed",
        checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        fetcher=robots_fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )

    assert len(report.results) == 1
    result = report.results[0]
    assert result.attempts == 1, "a robots disallow must never be retried"
    assert result.status == "robots_disallowed"

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT consecutive_failures, status FROM sources WHERE source_url = %s",
            (source_url,),
        )
        row = await cur.fetchone()
    assert row["consecutive_failures"] == 0
    assert row["status"] == "robots_disallowed"

    await _delete_test_rows(conn, source_url)


@requires_langgraph
@pytest.mark.freshness
async def test_graph_transient_fetch_error_retries_to_max_attempts_and_counts_as_a_failure(
    tmp_path, conn, database_url
):
    """Contrast case for the test above: a transient exception (not a robots disallow) DOES retry
    until max_attempts and DOES increment consecutive_failures.
    """
    source_url = "https://example.gov/freshness-graph-transient-fetch-error"
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    await _insert_test_row(conn, source_url, fetched_at=old_time)

    async def failing_fetcher(entry):
        del entry
        raise RuntimeError("simulated transient network failure")

    report = await run_refresh(
        settings=get_settings(),
        manifest=[{"url": source_url, "topic": topic, "title": "Transient Failure Page"}],
        raw_dir=tmp_path,
        run_id="test-run-transient-failure",
        checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        fetcher=failing_fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=3,
        backoff_seconds=0,
    )

    assert len(report.results) == 1
    result = report.results[0]
    assert result.attempts == 3
    assert result.status == "fetch_failed"

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT consecutive_failures, status FROM sources WHERE source_url = %s",
            (source_url,),
        )
        row = await cur.fetchone()
    assert row["consecutive_failures"] == 1
    assert row["status"] == "fetch_failed"

    await _delete_test_rows(conn, source_url)


@requires_langgraph
@pytest.mark.freshness
async def test_graph_records_404_http_status_distinct_from_a_timeout(tmp_path, conn, database_url):
    """DoD 4: a fetch failure carrying a real HTTP status (a 404, via httpx.HTTPStatusError) and
    one that does not (a ConnectTimeout) must be distinguishable afterward: last_http_status is 404
    in the first case and NULL in the second, and last_error differs.
    """
    url_404 = "https://example.gov/freshness-graph-404"
    url_timeout = "https://example.gov/freshness-graph-timeout"
    topic = "freshness_test"
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    await _insert_test_row(conn, url_404, fetched_at=old_time)
    await _insert_test_row(conn, url_timeout, fetched_at=old_time, heading="Other Heading")

    request_404 = httpx.Request("GET", url_404)
    response_404 = httpx.Response(404, request=request_404)

    async def fetcher(entry):
        if entry["url"] == url_404:
            raise httpx.HTTPStatusError("404 Not Found", request=request_404, response=response_404)
        raise httpx.ConnectTimeout(
            "connection timed out", request=httpx.Request("GET", url_timeout)
        )

    report = await run_refresh(
        settings=get_settings(),
        manifest=[
            {"url": url_404, "topic": topic, "title": "404 Page"},
            {"url": url_timeout, "topic": topic, "title": "Timeout Page"},
        ],
        raw_dir=tmp_path,
        run_id="test-run-404-vs-timeout",
        checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        fetcher=fetcher,
        conn_factory=make_conn_factory(database_url),
        embedder=StubEmbedder(dim=768),
        max_attempts=1,
        backoff_seconds=0,
    )

    by_url = {r.source_url: r for r in report.results}
    assert by_url[url_404].status == "fetch_failed"
    assert by_url[url_timeout].status == "fetch_failed"

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT source_url, last_http_status, last_error FROM sources "
            "WHERE source_url = ANY(%s)",
            ([url_404, url_timeout],),
        )
        rows = {r["source_url"]: r for r in await cur.fetchall()}

    assert rows[url_404]["last_http_status"] == 404
    assert rows[url_timeout]["last_http_status"] is None
    assert rows[url_404]["last_error"] != rows[url_timeout]["last_error"]

    await _delete_test_rows(conn, url_404, url_timeout)
