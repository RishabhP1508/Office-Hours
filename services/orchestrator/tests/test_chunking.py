"""Chunking tests against the corpus snapshots in RAW_SNAPSHOT_DIR.

No mocks: these tests load actual snapshot files (written by `python -m app.ingest`, or committed
directly as fixtures) and run the real chunker against them, per ARCHITECTURE.md's rule that the
chunker's input is always a snapshot file on disk.

Snapshots are located by source_url (read from each file's own frontmatter), never by filename.
ingest.py reuses an existing snapshot's filename when one already exists, which is why filenames on
this development machine happen to be readable (fixed_admission-studyinthestates-final-rule-faq.md
and friends). data/sources/raw/ is gitignored, so on a fresh clone it starts empty and ingest.py
mints its own filenames from the URL instead. A test keyed on a filename stem would only pass by
coincidence of what this machine's directory already contained; keying on source_url checks the
same identity ingest.py itself uses to decide whether a snapshot already exists for a URL.

This module runs against WHICHEVER directory RAW_SNAPSHOT_DIR names -- the real 14-source corpus
(data/sources/raw/, populated locally by `python -m app.ingest`) or the small, committed CI fixture
corpus (eval/fixtures/sources/, see .github/workflows/eval.yml). Most tests here check an invariant
of the chunker itself (every snapshot yields more than one chunk, every chunk has a non-empty
heading and a valid level, pre-heading content is captured, the fixed-admission FAQ's inverted
h2/h3 nesting parents correctly) and hold regardless of which corpus is loaded, so they run
unconditionally in both environments -- this is what makes a PR that breaks the chunker fail CI,
not just a local run.

A few assertions are genuinely full-corpus-only: an exact count of 14 snapshots matching
data/sources/sources.yaml, and the h4-only-page check parametrized over four specific real USCIS
URLs the small fixture corpus does not carry all of. Those are marked `@pytest.mark.full_corpus`
and deselected by the CI job's `pytest -m "not full_corpus"`; a plain `pytest -v` (the local,
full-stack command) still collects and enforces them exactly as before. The marker is only ever
used to deselect a check whose stated precondition (the real corpus) genuinely is not met; it is
never used to weaken or skip an assertion that could run in CI, and no assertion in this file was
loosened to make this split possible -- the fixed-admission FAQ's parenting invariant, in
particular, is checked in CI too, against headings the fixture corpus actually carries (see
test_fixed_admission_faq_transition_and_aud_parenting below), not skipped.
"""

import re
from pathlib import Path

import pytest
import yaml

from app.config import get_settings
from app.ingest import chunk_markdown, load_snapshot

RAW_DIR = Path(get_settings().RAW_SNAPSHOT_DIR)
SOURCES_MANIFEST_PATH = Path(get_settings().SOURCES_MANIFEST_PATH)

# The four pages that carry their real section boundaries in h4 with no h2 at all. Chunking on h2
# alone would collapse each of these into a single chunk. Identified by manifest URL (see module
# docstring for why not by filename). Only one of these four (h-1b-electronic-registration-process)
# is in the CI fixture corpus; the whole parametrized test stays full_corpus-marked rather than
# splitting out that one case, since "does every one of the four real h4-only pages behave" is the
# actual invariant this test protects.
H4_ONLY_PAGE_URLS = (
    "https://www.uscis.gov/working-in-the-united-states/temporary-workers/"
    "h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-"
    "f-1-status-for-eligible-students",
    "https://www.uscis.gov/working-in-the-united-states/temporary-workers/"
    "h-1b-specialty-occupations/h-1b-electronic-registration-process",
    "https://www.uscis.gov/working-in-the-united-states/h-1b-specialty-occupations",
    "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/"
    "optional-practical-training-extension-for-stem-students-stem-opt",
)

FIXED_ADMISSION_FAQ_URL = (
    "https://studyinthestates.dhs.gov/final-rule-establishing-a-fixed-time-period-of-admission-"
    "and-an-extension-of-stay-procedure-faq"
)
H1B_ELECTRONIC_REGISTRATION_URL = (
    "https://www.uscis.gov/working-in-the-united-states/temporary-workers/"
    "h-1b-specialty-occupations/h-1b-electronic-registration-process"
)


def _all_snapshot_paths() -> list[Path]:
    paths = sorted(RAW_DIR.glob("*.md"))
    assert paths, f"No snapshot files found in {RAW_DIR}; run `python -m app.ingest` first."
    return paths


def _build_snapshot_index() -> dict[str, tuple[Path, dict, str]]:
    """Map each snapshot's source_url (from its own frontmatter) to (path, frontmatter, body).

    A snapshot's identity is its source_url, not its filename: this is the same lookup ingest.py
    itself does (load_existing_snapshot_index) to decide whether a snapshot already exists for a
    manifest URL.
    """
    index: dict[str, tuple[Path, dict, str]] = {}
    for path in _all_snapshot_paths():
        frontmatter, body = load_snapshot(path)
        source_url = frontmatter.get("source_url")
        assert source_url, f"{path.name}: frontmatter has no source_url"
        assert source_url not in index, (
            f"source_url {source_url!r} appears in two snapshots: "
            f"{index[source_url][0].name} and {path.name}"
        )
        index[source_url] = (path, frontmatter, body)
    return index


def _chunks_for(path: Path) -> list[dict]:
    _, body = load_snapshot(path)
    return chunk_markdown(body)


@pytest.fixture(scope="module")
def snapshot_paths() -> list[Path]:
    return _all_snapshot_paths()


@pytest.fixture(scope="module")
def snapshot_index() -> dict[str, tuple[Path, dict, str]]:
    return _build_snapshot_index()


@pytest.mark.full_corpus
def test_fourteen_snapshots_present(snapshot_paths, snapshot_index):
    assert len(snapshot_paths) == 14, (
        f"Expected 14 snapshots in {RAW_DIR}, found {len(snapshot_paths)}: "
        f"{[p.name for p in snapshot_paths]}"
    )

    manifest = yaml.safe_load(SOURCES_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_urls = {entry["url"] for entry in manifest["sources"]}
    snapshot_urls = set(snapshot_index.keys())
    assert snapshot_urls == manifest_urls, (
        "Snapshot source_urls do not exactly match data/sources/sources.yaml.\n"
        f"In sources.yaml but no snapshot found: {manifest_urls - snapshot_urls}\n"
        f"Snapshot present but not in sources.yaml: {snapshot_urls - manifest_urls}"
    )


def test_every_snapshot_produces_more_than_one_chunk(snapshot_paths):
    for path in snapshot_paths:
        chunks = _chunks_for(path)
        assert len(chunks) > 1, (
            f"{path.name}: expected more than one chunk, got {len(chunks)}. "
            "A single chunk means the chunker collapsed the whole page (fixed character count or "
            "h2-only splitting) instead of chunking by section."
        )


@pytest.mark.full_corpus
@pytest.mark.parametrize("url", H4_ONLY_PAGE_URLS, ids=lambda u: u.rsplit("/", 1)[-1])
def test_h4_only_pages_produce_at_least_four_chunks(url, snapshot_index):
    assert url in snapshot_index, f"No snapshot found for source_url {url!r} in {RAW_DIR}"
    _, _, body = snapshot_index[url]
    chunks = chunk_markdown(body)
    assert len(chunks) >= 4, (
        f"{url}: expected at least 4 chunks (this page has no h2 at all; its real section "
        f"boundaries are h4), got {len(chunks)}."
    )


def test_every_chunk_has_a_non_empty_heading_and_valid_level(snapshot_paths):
    for path in snapshot_paths:
        for chunk in _chunks_for(path):
            assert (
                chunk["heading"] and chunk["heading"].strip()
            ), f"{path.name}: found a chunk with an empty heading: {chunk!r}"
            assert chunk["level"] in (
                1,
                2,
                3,
                4,
            ), f"{path.name}: chunk {chunk['heading']!r} has an invalid level {chunk['level']!r}"


def _chunk_by_heading(chunks: list[dict], heading: str) -> dict:
    matches = [c for c in chunks if c["heading"] == heading]
    headings = [c["heading"] for c in chunks]
    assert matches, f"No chunk found with heading {heading!r} among: {headings}"
    assert len(matches) == 1, f"Multiple chunks found with heading {heading!r}"
    return matches[0]


@pytest.mark.full_corpus
def test_fixed_admission_faq_parenting_inverts_correctly(snapshot_index):
    """The FAQ's questions are h2 while the group labels holding them are h3, sitting under empty h2
    super-labels. A naive level-number stack pops the h3 group the moment the next h2 question
    arrives and mis-parents every answer on the page; parenting must come from document order and
    empty-body headings instead.

    Full-corpus-only: "Departure Period for F Students" is one of the group labels the CI fixture
    corpus trims away to keep its size small (see eval/fixtures/sources/). The invariant itself is
    also checked in CI, against headings the fixture does carry -- see
    test_fixed_admission_faq_transition_and_aud_parenting below.
    """
    assert (
        FIXED_ADMISSION_FAQ_URL in snapshot_index
    ), f"No snapshot found for source_url {FIXED_ADMISSION_FAQ_URL!r} in {RAW_DIR}"
    _, _, body = snapshot_index[FIXED_ADMISSION_FAQ_URL]
    chunks = chunk_markdown(body)

    departure = _chunk_by_heading(chunks, "What is the new departure period for F students?")
    assert departure["parent"] == "Departure Period for F Students"

    transition = _chunk_by_heading(
        chunks,
        "If I am a current student admitted under duration of status, do I need to apply for an "
        "extension of stay?",
    )
    assert transition["parent"] == "Transition Period"

    aud = _chunk_by_heading(chunks, "What does the AUD mean?")
    assert aud["parent"] == "Understanding the Admit Until Date (AUD)"


def test_fixed_admission_faq_transition_and_aud_parenting(snapshot_index):
    """Fixture-corpus-compatible companion to test_fixed_admission_faq_parenting_inverts_correctly
    above: the same document-order, empty-heading parenting invariant, checked against two headings
    present verbatim in BOTH the full corpus (data/sources/raw/) and the small CI fixture corpus
    (eval/fixtures/sources/), so this one test runs -- and can actually catch a chunker regression
    -- in CI, not just locally. Nothing here is a weaker version of the assertion above; both
    headings and both expected parents are exactly what the full-corpus test also expects of them.
    """
    assert (
        FIXED_ADMISSION_FAQ_URL in snapshot_index
    ), f"No snapshot found for source_url {FIXED_ADMISSION_FAQ_URL!r} in {RAW_DIR}"
    _, _, body = snapshot_index[FIXED_ADMISSION_FAQ_URL]
    chunks = chunk_markdown(body)

    transition = _chunk_by_heading(
        chunks,
        "If I am a current student admitted under duration of status, do I need to apply for an "
        "extension of stay?",
    )
    assert transition["parent"] == "Transition Period"

    aud = _chunk_by_heading(chunks, "What does the AUD mean?")
    assert aud["parent"] == "Understanding the Admit Until Date (AUD)"


def test_h1b_electronic_registration_preheading_table_is_captured(snapshot_index):
    """The H-1B electronic registration page has substantive content, including a historical
    registration/selection data table, above its first real heading. That content must land in a
    chunk, attributed to the page title, never dropped. Runs against both the real corpus (the
    full ~65-line pre-heading block) and the CI fixture corpus (a trimmed version that still keeps
    the FY2021 and FY2026 total-registration figures and the same page title), since the fixture
    was built to preserve exactly this invariant, not to skip checking it.
    """
    assert (
        H1B_ELECTRONIC_REGISTRATION_URL in snapshot_index
    ), f"No snapshot found for source_url {H1B_ELECTRONIC_REGISTRATION_URL!r} in {RAW_DIR}"
    path, _, body = snapshot_index[H1B_ELECTRONIC_REGISTRATION_URL]
    chunks = chunk_markdown(body)

    matches = [c for c in chunks if "274,237" in c["text"] and "358,737" in c["text"]]
    assert matches, (
        "No chunk contains both the FY2021 (274,237) and FY2026 (358,737) total registration "
        "figures from the historical data table; pre-heading content may have been dropped."
    )
    assert len(matches) == 1, "The historical table figures should live in exactly one chunk."

    # Derive the expected title independently from the raw file's own h1 line, rather than
    # hardcoding the string, so this checks the chunker's behavior and not a copied literal.
    h1_match = re.search(r"^#\s+(\S.*)$", body, flags=re.MULTILINE)
    assert h1_match, f"{path.name}: could not find an h1 line to compare against"
    expected_title = h1_match.group(1).strip()

    assert matches[0]["heading"] == expected_title, (
        "The chunk holding the pre-heading historical table should be attributed to the page "
        f"title {expected_title!r}, got heading={matches[0]['heading']!r}"
    )
    assert matches[0]["level"] == 1
