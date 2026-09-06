"""Ingestion: fetch -> snapshot -> chunk -> embed -> store.

Runnable as `python -m app.ingest`. Reads data/sources/sources.yaml, fetches each page over plain
HTTP (no browser engine), normalizes it to a markdown snapshot with a YAML frontmatter header,
writes the snapshot to data/sources/raw/, chunks the snapshot by section, embeds each chunk, and
stores it in `documents`. Re-running is idempotent: a source's old rows are deleted before its new
chunks are inserted, in one transaction per source.

The chunking functions below (`parse_frontmatter`, `chunk_markdown`, and friends) are pure and
side-effect-free so tests/test_chunking.py can call them directly against the snapshot files on
disk, per ARCHITECTURE.md: "the chunker's input is always a snapshot file, never a live URL."
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import psycopg
import yaml
from bs4 import BeautifulSoup
from bs4.element import Tag
from dateutil import parser as dateutil_parser
from markdownify import markdownify as _markdownify
from pgvector import Vector
from pgvector.psycopg import register_vector_async

from app.config import Settings, get_settings
from app.providers.embeddings import get_embedder

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ingest")

# ---------------------------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?", re.DOTALL)

# Keys ingest.py itself computes on every run. Any other key found in an existing snapshot's
# frontmatter (heading_note, rule_effective_date, federal_register, note, ...) is carried forward
# unchanged: see ARCHITECTURE.md's note that the prep step's annotations must never be lost.
BASE_FRONTMATTER_KEYS = (
    "source_url",
    "resolved_url",
    "title",
    "fetched_at",
    "page_last_updated",
    "topic",
)


def parse_frontmatter(raw_text: str) -> tuple[dict, str]:
    """Split a snapshot file into its YAML frontmatter dict and its markdown body."""
    match = _FRONTMATTER_RE.match(raw_text)
    if not match:
        return {}, raw_text
    frontmatter = yaml.safe_load(match.group(1)) or {}
    body = raw_text[match.end() :]
    return frontmatter, body


def render_snapshot(frontmatter: dict, body: str) -> str:
    fm_text = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return f"---\n{fm_text}---\n\n{body.lstrip(chr(10))}"


def build_frontmatter(existing: dict, computed: dict) -> dict:
    """Merge computed (fresh, authoritative) fields over an existing frontmatter dict.

    Base keys (BASE_FRONTMATTER_KEYS) always come from `computed`, even if that means dropping one
    (e.g. resolved_url when a redirect no longer applies). Every other key in `existing` is an
    unknown key the ingest script did not produce, and is preserved verbatim.
    """
    result: dict = {}
    for key in BASE_FRONTMATTER_KEYS:
        if key in computed and computed[key] is not None:
            result[key] = computed[key]
    for key, value in existing.items():
        if key in BASE_FRONTMATTER_KEYS:
            continue
        result[key] = value
    return result


def load_snapshot(path: Path) -> tuple[dict, str]:
    return parse_frontmatter(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------
# Chunking (pure, side-effect-free; called directly by tests/test_chunking.py)
# ---------------------------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,4})[ \t]+(\S.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_EMPHASIS_WRAP_RE = re.compile(r"^(\*\*|__)(.+)\1$")
_LINK_WRAP_RE = re.compile(r"^\[(.+)\]\([^)]*\)$")


@dataclass
class _Heading:
    line_idx: int
    level: int
    text: str


def _clean_heading_text(text: str) -> str:
    """Strip markdown noise wrapping the whole heading.

    Handles two cases, looped until stable so combinations of the two resolve correctly (a link
    wrapped in emphasis, or emphasis inside a link):
    - Emphasis: an HTML <strong> around a heading's text converts to "**text**", which is
      redundant noise on something already rendered as a heading.
    - A self-anchor link: some pages (e.g. the ICE travel FAQ) wrap each heading's own text in a
      link to its own anchor, e.g. "[What if my visa expired?](#what-if-my-visa-expired)". A
      citation label has to be the readable heading text, not markdown link source.
    """
    changed = True
    while changed:
        changed = False
        match = _EMPHASIS_WRAP_RE.match(text)
        if match:
            text = match.group(2).strip()
            changed = True
            continue
        match = _LINK_WRAP_RE.match(text)
        if match:
            text = match.group(1).strip()
            changed = True
            continue
    return text


def _find_headings(lines: list[str]) -> list[_Heading]:
    """Locate heading lines (level 1-4), ignoring anything inside a fenced code block.

    A heading whose text is empty once cleaned (e.g. a bare "###" with nothing after it, or
    markdown that fully collapses away) is skipped entirely rather than accepted as an empty
    label: an empty group label would silently become `current_label` for every content chunk
    that follows it in chunk_markdown, corrupting their `parent`.
    """
    headings: list[_Heading] = []
    in_fence = False
    for idx, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            text = _clean_heading_text(match.group(2).strip())
            if not text:
                continue
            headings.append(_Heading(line_idx=idx, level=level, text=text))
    return headings


def _breadcrumb(page_title: str, parent: str | None, heading: str) -> str:
    parts = [page_title]
    if parent:
        parts.append(parent)
    if heading != page_title:
        parts.append(heading)
    return " > ".join(parts)


def chunk_markdown(body: str) -> list[dict]:
    """Chunk a markdown document by section (h2-h4), never by a fixed character count.

    Rules (see ARCHITECTURE.md "Corpus and chunking" for the reasoning):
    - The document's first heading is the page title, not a section boundary, whatever its level
      (usually h1; a few pages have no h1 at all and title themselves with an h2). Body text above
      the first following h2/h3/h4 heading is captured as its own chunk, attributed to the page
      title. It is never dropped.
    - A heading whose body (the text before the next heading) is empty is a GROUP LABEL, not a
      content section: no chunk is emitted for it, but it becomes the "current label" that content
      chunks below it point to as `parent`, in document order. Level number plays no role in this;
      only "is there a preceding label, and what does it say" does.
    - Each content chunk's `text` is its breadcrumb line followed by its body, so the embedding and
      the model both see which group a question belongs to.
    """
    lines = body.split("\n")
    all_headings = _find_headings(lines)

    # The first heading in the document is the page title, whatever its level. On USCIS/ICE pages
    # this is a real h1; some Study in the States pages have no h1 at all and mark the page title
    # with an h2 instead. Either way it is excluded from the boundary list below, and everything
    # after it (of levels 2-4) is a real section boundary, even a later heading at that same level.
    if all_headings:
        title_heading = all_headings[0]
        page_title = title_heading.text
        first_boundary_idx = title_heading.line_idx + 1
        remaining_headings = all_headings[1:]
    else:
        page_title = "Untitled"
        first_boundary_idx = 0
        remaining_headings = []

    boundaries = [h for h in remaining_headings if h.level in (2, 3, 4)]

    chunks: list[dict] = []

    # Pre-heading content: everything between the page title (h1) and the first h2/h3/h4 boundary,
    # never dropped (the H-1B electronic registration page has ~65 lines and a 6-row historical
    # table here).
    pre_end = boundaries[0].line_idx if boundaries else len(lines)
    pre_text = "\n".join(lines[first_boundary_idx:pre_end]).strip()
    if pre_text:
        chunks.append(
            {
                "heading": page_title,
                "level": 1,
                "parent": None,
                "breadcrumb": _breadcrumb(page_title, None, page_title),
                "text": f"{_breadcrumb(page_title, None, page_title)}\n\n{pre_text}",
            }
        )

    current_label: str | None = None
    for i, heading in enumerate(boundaries):
        body_start = heading.line_idx + 1
        body_end = boundaries[i + 1].line_idx if i + 1 < len(boundaries) else len(lines)
        section_body = "\n".join(lines[body_start:body_end]).strip()

        if not section_body:
            # Empty body => group label. Its own parent is whatever label preceded it; it becomes
            # the new current label for everything that follows, in document order.
            current_label = heading.text
            continue

        breadcrumb = _breadcrumb(page_title, current_label, heading.text)
        chunks.append(
            {
                "heading": heading.text,
                "level": heading.level,
                "parent": current_label,
                "breadcrumb": breadcrumb,
                "text": f"{breadcrumb}\n\n{section_body}",
            }
        )

    return chunks


# ---------------------------------------------------------------------------------------------
# HTML -> markdown normalization
# ---------------------------------------------------------------------------------------------

_NOISE_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form")
_NOISE_KEYWORDS = (
    "breadcrumb",
    "skip",
    "menu",
    "sidebar",
    "social",
    "share",
    "feedback",
    "pagination",
    "footer",
)

_DATE_PATTERNS = (
    # USCIS: "Last Reviewed/Updated: MM/DD/YYYY"
    re.compile(r"Last\s+Reviewed/Updated\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
    # Study in the States / ICE: "Last updated: Month D, YYYY" or "Updated: MM/DD/YYYY"
    re.compile(
        r"(?:Last\s+updated|Updated)\s*:?\s*"
        r"([A-Za-z]+\.?\s+\d{1,2},\s*\d{4}|\d{1,2}/\d{1,2}/\d{4})",
        re.IGNORECASE,
    ),
)


def select_main_container(soup: BeautifulSoup) -> Tag:
    for candidate in (
        soup.find("main"),
        soup.find("article"),
        soup.find(attrs={"role": "main"}),
        soup.find(class_="field--name-body"),
        soup.find(class_="region-content"),
        soup.find("body"),
    ):
        if candidate is not None:
            return candidate
    return soup


def remove_noise(container: Tag) -> None:
    for tag in container.find_all(_NOISE_TAGS):
        tag.decompose()
    for tag in container.find_all(True):
        if tag.decomposed:
            continue
        haystack_parts = [tag.get("id") or ""]
        haystack_parts.extend(tag.get("class") or [])
        haystack = " ".join(haystack_parts).lower()
        if any(keyword in haystack for keyword in _NOISE_KEYWORDS):
            tag.decompose()


def promote_definition_lists(container: Tag) -> None:
    """Turn <dl>/<dt>/<dd> accordions into real headings and body text.

    Several Study in the States pages render their FAQ-style leaf sections as a CKEditor
    accordion widget (<dl class="ckeditor-accordion"><dt>question</dt><dd>answer</dd>...</dl>)
    rather than heading tags. Left alone, markdownify turns a <dt>/<dd> pair into a definition-list
    line ("question\n:   answer") that the section chunker cannot see as a boundary at all, which
    silently collapses the whole accordion into one chunk (or one chunk per real heading around
    it). Renaming each <dt> to an <h2> and unwrapping each <dd> restores real section boundaries
    without depending on a browser to render the widget.
    """
    for dl in container.find_all("dl"):
        for dt in dl.find_all("dt", recursive=False):
            dt.name = "h2"
        for dd in dl.find_all("dd", recursive=False):
            dd.unwrap()
        dl.unwrap()


def html_to_markdown(container: Tag) -> str:
    markdown = _markdownify(str(container), heading_style="ATX", bullets="-")
    # Collapse runs of 3+ blank lines that markdownify tends to leave behind.
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip() + "\n"


def extract_page_last_updated(full_text: str) -> date | None:
    for pattern in _DATE_PATTERNS:
        match = pattern.search(full_text)
        if match:
            try:
                return dateutil_parser.parse(match.group(1)).date()
            except (ValueError, OverflowError):
                continue
    return None


def extract_title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return None


# ---------------------------------------------------------------------------------------------
# Manifest, robots.txt, and rate limiting
# ---------------------------------------------------------------------------------------------


def read_manifest(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["sources"]


class RobotsCache:
    """Fetches and caches one robots.txt per host."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str):
        self._client = client
        self._user_agent = user_agent
        self._parsers: dict[str, RobotFileParser] = {}

    async def _get_parser(self, host_root: str) -> RobotFileParser:
        if host_root not in self._parsers:
            parser = RobotFileParser()
            try:
                response = await self._client.get(
                    f"{host_root}/robots.txt", headers={"User-Agent": self._user_agent}
                )
                if response.status_code >= 400:
                    parser.parse([])
                else:
                    parser.parse(response.text.splitlines())
            except httpx.HTTPError:
                logger.warning("Could not fetch robots.txt for %s; allowing by default", host_root)
                parser.parse([])
            self._parsers[host_root] = parser
        return self._parsers[host_root]

    async def can_fetch(self, url: str) -> bool:
        parts = urlsplit(url)
        host_root = f"{parts.scheme}://{parts.netloc}"
        parser = await self._get_parser(host_root)
        return parser.can_fetch(self._user_agent, url)


class HostRateLimiter:
    """Sleeps CRAWL_DELAY_SECONDS between requests to the same host."""

    def __init__(self, delay_seconds: float):
        self._delay = delay_seconds
        self._last_request: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last_request.get(host)
        if last is not None:
            elapsed = now - last
            if elapsed < self._delay:
                await asyncio.sleep(self._delay - elapsed)
        self._last_request[host] = time.monotonic()


# ---------------------------------------------------------------------------------------------
# Snapshot filenames
# ---------------------------------------------------------------------------------------------


def load_existing_snapshot_index(raw_dir: Path) -> dict[str, Path]:
    """Map each existing snapshot's source_url to its file path, so re-running reuses filenames."""
    index: dict[str, Path] = {}
    if not raw_dir.exists():
        return index
    for path in sorted(raw_dir.glob("*.md")):
        try:
            frontmatter, _ = load_snapshot(path)
        except Exception:  # noqa: BLE001 - a malformed snapshot should not stop the whole run
            logger.warning("Could not parse frontmatter for existing snapshot %s", path)
            continue
        source_url = frontmatter.get("source_url")
        if source_url:
            index[source_url] = path
    return index


def _domain_hint(netloc: str) -> str:
    """A short, readable label for a host: uscis, ice, studyinthestates.

    A plain `netloc.split(".")[0]` gives "www" for both www.uscis.gov and www.ice.gov, which is
    useless for telling snapshots apart in a directory listing. Stripping a leading "www." first
    gets the registrable name instead.
    """
    host = netloc.lower()
    if host.startswith("www."):
        host = host[len("www.") :]
    return host.split(".")[0]


def mint_snapshot_filename(topic: str, url: str, raw_dir: Path) -> Path:
    parts = urlsplit(url)
    segment = parts.path.rstrip("/").rsplit("/", 1)[-1] or parts.netloc
    slug = re.sub(r"[^a-z0-9]+", "-", segment.lower()).strip("-")
    domain_hint = _domain_hint(parts.netloc)
    return raw_dir / f"{topic}-{domain_hint}-{slug}.md"


# ---------------------------------------------------------------------------------------------
# The ingest run
# ---------------------------------------------------------------------------------------------


def _parse_iso_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return dateutil_parser.parse(str(value)).date()


@dataclass
class FetchedPage:
    """One manifest entry's page, fetched and normalized to markdown -- no snapshot file involved.

    `resolved_url` follows the same rule the snapshot frontmatter always has: `None` when the fetch
    landed on the same URL the manifest listed (no redirect happened), the landed-on URL otherwise.
    """

    resolved_url: str | None
    title: str | None
    page_last_updated: date | None
    body_markdown: str


async def fetch_page(
    client: httpx.AsyncClient,
    robots: RobotsCache,
    rate_limiter: HostRateLimiter,
    entry: dict,
) -> FetchedPage | None:
    """Fetch one manifest entry's URL and normalize it to markdown.

    Returns `None` when robots.txt disallows fetching this URL -- a permanent, not a transient,
    reason not to have a page. Raises whatever httpx.HTTPError (or `response.raise_for_status()`)
    raises on a connection failure or a non-2xx status; callers (`_fetch_and_snapshot` here, and
    app/recrawl.py's refresh graph) decide how to handle that.

    Pulled out of `_fetch_and_snapshot` (Phase 5) so app/recrawl.py can reuse the exact same fetch
    normalization the ingest CLI uses, instead of forking a second copy of it. `_fetch_and_snapshot`
    itself is unchanged in behavior: it now calls this function and only does the snapshot-writing
    part that follows.
    """
    url = entry["url"]

    host = urlsplit(url).netloc
    await rate_limiter.wait(host)

    if not await robots.can_fetch(url):
        logger.warning("SKIP (robots.txt disallows fetching): %s", url)
        return None

    response = await client.get(url)
    response.raise_for_status()
    resolved_url = str(response.url)

    soup = BeautifulSoup(response.text, "lxml")
    full_text = soup.get_text("\n")
    page_last_updated = extract_page_last_updated(full_text)
    if page_last_updated is None:
        logger.warning("No page_last_updated date found on: %s", url)

    page_title = extract_title(soup) or entry.get("title")

    container = select_main_container(soup)
    remove_noise(container)
    promote_definition_lists(container)
    body_markdown = html_to_markdown(container)

    return FetchedPage(
        resolved_url=resolved_url if resolved_url != url else None,
        title=page_title,
        page_last_updated=page_last_updated,
        body_markdown=body_markdown,
    )


async def _fetch_and_snapshot(
    client: httpx.AsyncClient,
    robots: RobotsCache,
    rate_limiter: HostRateLimiter,
    entry: dict,
    existing_index: dict[str, Path],
    raw_dir: Path,
) -> Path | None:
    url = entry["url"]
    topic = entry["topic"]

    fetched = await fetch_page(client, robots, rate_limiter, entry)
    if fetched is None:
        return None

    snapshot_path = existing_index.get(url) or mint_snapshot_filename(topic, url, raw_dir)
    existing_frontmatter: dict = {}
    if snapshot_path.exists():
        existing_frontmatter, _ = load_snapshot(snapshot_path)

    computed_frontmatter = {
        "source_url": url,
        "resolved_url": fetched.resolved_url,
        "title": fetched.title,
        "fetched_at": date.today().isoformat(),
        "page_last_updated": (
            fetched.page_last_updated.isoformat() if fetched.page_last_updated else None
        ),
        "topic": topic,
    }
    frontmatter = build_frontmatter(existing_frontmatter, computed_frontmatter)

    raw_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(render_snapshot(frontmatter, fetched.body_markdown), encoding="utf-8")
    return snapshot_path


async def _embed_and_store(
    conn: psycopg.AsyncConnection,
    embedder,
    source_url: str,
    resolved_url: str | None,
    page_last_updated: date | None,
    chunks: list[dict],
    rule_effective_date: date | None = None,
    now: datetime | None = None,
    mark_changed: bool = False,
) -> None:
    """`rule_effective_date` is the curator annotation carried in a snapshot's own frontmatter
    (e.g. `rule_effective_date: 2026-09-15` on both fixed_admission snapshots), never computed here
    -- see infra/sql/init.sql's comment on the column. `now` defaults to the real current time; it
    is an explicit parameter so app/recrawl.py::reindex_source (Phase 5) can pass one `now` value
    for both `fetched_at` and `last_verified_at`, deterministically, instead of two separate calls
    to datetime.now(UTC) that could disagree by a few microseconds.

    Phase 7: `documents.source_url` is a foreign key into `sources`, so that row has to exist
    before any chunk referencing it can be inserted -- the upsert below runs FIRST, inside the same
    transaction as the delete-then-insert of this source's chunks, so a killed/rolled-back run never
    leaves a chunk pointing at a `sources` row that was never committed (and never leaves a
    `sources` row upserted without its chunks, either -- the whole thing is one transaction).

    `mark_changed` is False for every ordinary ingest/verify-only call (a first-time ingest, or a
    re-embed of an unchanged page): `last_changed_at`/`change_count` should move only when a
    re-crawl found the content actually different, which is exactly what
    app/recrawl.py::reindex_source (the only caller that ever re-runs this for a source already in
    `sources`) sets it True for.
    """
    if now is None:
        now = datetime.now(UTC)
    texts = [chunk["text"] for chunk in chunks]
    vectors = await embedder.embed(texts)

    async with conn.transaction():
        async with conn.cursor() as cur:
            # ONE upsert statement, not two near-identical copies differing only in the
            # mark_changed branch: `last_changed_at`/`change_count` are the only fields whose
            # values actually depend on `mark_changed`, so that dependency is expressed as a SQL
            # CASE inline rather than as two statements that could drift out of sync with each
            # other over time -- this function is the single place that owns the delete-and-replace
            # invariant, so it should not itself carry a copy-paste risk.
            await cur.execute(
                """
                INSERT INTO sources
                    (source_url, resolved_url, page_last_updated, fetched_at, last_verified_at,
                     last_changed_at, last_success_at, change_count, consecutive_failures,
                     last_error, last_http_status, status)
                VALUES (%(source_url)s, %(resolved_url)s, %(page_last_updated)s, %(now)s, %(now)s,
                        %(now)s, %(now)s, 0, 0, NULL, NULL, 'ok')
                ON CONFLICT (source_url) DO UPDATE SET
                    resolved_url = EXCLUDED.resolved_url,
                    page_last_updated = EXCLUDED.page_last_updated,
                    fetched_at = EXCLUDED.fetched_at,
                    last_verified_at = EXCLUDED.last_verified_at,
                    last_success_at = EXCLUDED.last_success_at,
                    status = 'ok',
                    consecutive_failures = 0,
                    last_error = NULL,
                    last_http_status = NULL,
                    last_changed_at = CASE WHEN %(mark_changed)s THEN EXCLUDED.last_changed_at
                                            ELSE sources.last_changed_at END,
                    change_count = sources.change_count
                        + CASE WHEN %(mark_changed)s THEN 1 ELSE 0 END
                """,
                {
                    "source_url": source_url,
                    "resolved_url": resolved_url,
                    "page_last_updated": page_last_updated,
                    "now": now,
                    "mark_changed": mark_changed,
                },
            )

            await cur.execute("DELETE FROM documents WHERE source_url = %s", (source_url,))
            for chunk, vector in zip(chunks, vectors, strict=True):
                await cur.execute(
                    """
                    INSERT INTO documents
                        (content, source_url, section_heading, heading_level,
                         rule_effective_date, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chunk["text"],
                        source_url,
                        chunk["heading"],
                        chunk["level"],
                        rule_effective_date,
                        Vector(vector),
                    ),
                )


async def _ingest_from_manifest(
    conn: psycopg.AsyncConnection, embedder, settings: Settings, raw_dir: Path
) -> tuple[int, int]:
    """Phase 0's path: fetch every URL in SOURCES_MANIFEST_PATH, snapshot, chunk, embed, store.

    Returns (total_chunks, total_sources).
    """
    manifest = read_manifest(Path(settings.SOURCES_MANIFEST_PATH))
    existing_index = load_existing_snapshot_index(raw_dir)

    total_chunks = 0
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": settings.USER_AGENT},
    ) as client:
        robots = RobotsCache(client, settings.USER_AGENT)
        rate_limiter = HostRateLimiter(settings.CRAWL_DELAY_SECONDS)

        for entry in manifest:
            url = entry["url"]
            snapshot_path = await _fetch_and_snapshot(
                client, robots, rate_limiter, entry, existing_index, raw_dir
            )
            if snapshot_path is None:
                continue

            frontmatter, body = load_snapshot(snapshot_path)
            chunks = chunk_markdown(body)

            await _embed_and_store(
                conn,
                embedder,
                source_url=frontmatter["source_url"],
                resolved_url=frontmatter.get("resolved_url"),
                page_last_updated=_parse_iso_date(frontmatter.get("page_last_updated")),
                rule_effective_date=_parse_iso_date(frontmatter.get("rule_effective_date")),
                chunks=chunks,
            )

            print(f"{url} -> {len(chunks)} chunks")
            total_chunks += len(chunks)

    return total_chunks, len(manifest)


async def _ingest_from_snapshots(
    conn: psycopg.AsyncConnection, embedder, raw_dir: Path
) -> tuple[int, int]:
    """Chunk and embed every snapshot already present in raw_dir, skipping fetch entirely.

    Used for INGEST_MODE=snapshot (the CI fixture corpus, eval/fixtures/sources -- see
    docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md). The fixture files are themselves
    valid snapshots in the exact format _fetch_and_snapshot writes (YAML frontmatter + markdown
    body), so there is nothing left to fetch. This reuses load_snapshot, chunk_markdown, and
    _embed_and_store unchanged -- only how a snapshot is obtained differs from
    _ingest_from_manifest, never the chunking or storage logic itself.

    Returns (total_chunks, total_sources).
    """
    total_chunks = 0
    paths = sorted(raw_dir.glob("*.md"))
    for path in paths:
        frontmatter, body = load_snapshot(path)
        source_url = frontmatter.get("source_url")
        if not source_url:
            raise RuntimeError(f"{path}: snapshot has no source_url in its frontmatter")
        chunks = chunk_markdown(body)

        await _embed_and_store(
            conn,
            embedder,
            source_url=source_url,
            resolved_url=frontmatter.get("resolved_url"),
            page_last_updated=_parse_iso_date(frontmatter.get("page_last_updated")),
            rule_effective_date=_parse_iso_date(frontmatter.get("rule_effective_date")),
            chunks=chunks,
        )

        print(f"{source_url} -> {len(chunks)} chunks (from snapshot {path.name})")
        total_chunks += len(chunks)

    return total_chunks, len(paths)


async def run_ingest() -> None:
    settings: Settings = get_settings()
    raw_dir = Path(settings.RAW_SNAPSHOT_DIR)
    embedder = get_embedder(settings)

    conn = await psycopg.AsyncConnection.connect(settings.DATABASE_URL)
    await register_vector_async(conn)
    try:
        if settings.INGEST_MODE == "snapshot":
            total_chunks, total_sources = await _ingest_from_snapshots(conn, embedder, raw_dir)
        elif settings.INGEST_MODE == "fetch":
            total_chunks, total_sources = await _ingest_from_manifest(
                conn, embedder, settings, raw_dir
            )
        else:
            raise ValueError(f"Unknown INGEST_MODE: {settings.INGEST_MODE!r}")
    finally:
        await conn.close()

    print(f"TOTAL: {total_chunks} chunks across {total_sources} sources")


def main() -> None:
    asyncio.run(run_ingest())


if __name__ == "__main__":
    main()
