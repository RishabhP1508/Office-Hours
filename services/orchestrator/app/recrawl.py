"""The scheduled refresh job: re-crawl the sources manifest, diff each page against what is
currently indexed, and re-index only the sources whose content meaningfully changed.

Runnable as `python -m app.recrawl` (see main() / .github/workflows/recrawl.yml). This module is
built with LangGraph (a StateGraph, one invocation per source, checkpointed to a local sqlite file
so a killed run resumes instead of re-fetching every source from scratch) -- but LangGraph itself
(and everything it pulls in transitively: langchain-core, langgraph-checkpoint,
langgraph-prebuilt, langgraph-sdk, langchain-protocol, ormsgpack, orjson, websockets, xxhash,
sqlite-vec) is installed ONLY by the orchestrator's `[freshness]` extra
(services/orchestrator/pyproject.toml), which is installed ONLY by
.github/workflows/recrawl.yml's scheduled job and by a developer running this module locally --
NEVER by services/orchestrator/Dockerfile (the image that serves /query) and NEVER by CI's
invariant gate (.github/workflows/eval.yml's `pull_request` job).

That isolation is enforced by NEVER importing `langgraph.*` at module scope here. Every
`from langgraph...` import lives inside the one function that actually needs it
(`build_refresh_graph` and `run_refresh`), the same lazy-import pattern eval/metrics.py already
uses for `ragas`. Merely importing `app.recrawl` -- as
services/orchestrator/tests/test_freshness.py's own dependency-isolation test does, in a fresh
subprocess -- must never require langgraph to be installed. Everything above that line (the pure
diff/classify functions, the DB bookkeeping helpers, the dataclasses) is plain Python with no
LangGraph dependency at all, and is fully testable without the `[freshness]` extra.

Every value that ends up inside the graph's own State (the TypedDict below) must be
JSON/msgpack-serializable, since LangGraph's checkpointer persists it to disk between steps: no
dataclass instances, no `datetime`/`date` objects. Dates and the serialized ChangeVerdict travel as
ISO strings and plain dicts instead.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

import httpx
import psycopg
from pgvector.psycopg import register_vector_async

from app.config import Settings, get_settings
from app.ingest import (
    FetchedPage,
    HostRateLimiter,
    RobotsCache,
    _embed_and_store,
    _parse_iso_date,
    build_frontmatter,
    chunk_markdown,
    fetch_page,
    load_existing_snapshot_index,
    load_snapshot,
    mint_snapshot_filename,
    read_manifest,
    render_snapshot,
)
from app.providers.embeddings import Embedder, get_embedder

# ---------------------------------------------------------------------------------------------
# normalize_for_diff: line-based normalization that strips boilerplate before anything is compared
# ---------------------------------------------------------------------------------------------

_QUOTE_DASH_TRANSLATION = str.maketrans(
    {
        "‘": "'",  # left single quote
        "’": "'",  # right single quote
        "“": '"',  # left double quote
        "”": '"',  # right double quote
        "–": "-",  # en dash
        "—": "-",  # em dash
        "…": "...",  # ellipsis
    }
)

_MARKDOWN_LINK_ONLY_RE = re.compile(r"^\[[^\]]*\]\([^)]*\)$")
_LAST_UPDATED_RE = re.compile(r"^(?:last\s+updated|last\s+reviewed/updated)\s*:?", re.IGNORECASE)
_SKIP_TO_RE = re.compile(r"^skip\s+to\b", re.IGNORECASE)
_BARE_HR_RE = re.compile(r"^[-*_]{3,}$")
_BOILERPLATE_EXACT_LINES = frozenset({"return to top", "print", "share"})
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _is_boilerplate_line(line: str) -> bool:
    """A line that carries no substantive content: empty, a bare navigation/timestamp line, or a
    line that is only a markdown link (a "read more" / "skip to section" style link with nothing
    else on the line).
    """
    stripped = line.strip()
    if not stripped:
        return True
    if _MARKDOWN_LINK_ONLY_RE.match(stripped):
        return True
    if _LAST_UPDATED_RE.match(stripped):
        return True
    if _SKIP_TO_RE.match(stripped):
        return True
    if _BARE_HR_RE.match(stripped):
        return True
    if stripped.lower() in _BOILERPLATE_EXACT_LINES:
        return True
    return False


def normalize_for_diff(body: str) -> list[str]:
    """Normalize a markdown body into the ordered list of lines a diff should actually compare.

    - NFKC-normalize each line, then map curly quotes/dashes/ellipsis to their ASCII equivalents,
      so a page re-rendering the same text with different Unicode punctuation does not look like a
      content change.
    - Drop boilerplate/navigation/timestamp lines entirely (see `_is_boilerplate_line`): empty
      lines, a line that is only a markdown link, "Last updated: ..." / "Last Reviewed/Updated:
      ..." lines, "Skip to ..." lines, and bare "Return to top" / "Print" / "Share" / horizontal-
      rule lines.
    - Collapse internal whitespace runs to a single space and strip each surviving line.

    Returns the surviving lines, in document order.
    """
    kept: list[str] = []
    for raw_line in body.split("\n"):
        normalized = unicodedata.normalize("NFKC", raw_line).translate(_QUOTE_DASH_TRANSLATION)
        if _is_boilerplate_line(normalized):
            continue
        collapsed = _WHITESPACE_RUN_RE.sub(" ", normalized).strip()
        if not collapsed:
            continue
        kept.append(collapsed)
    return kept


# ---------------------------------------------------------------------------------------------
# classify_change: unchanged / cosmetic / meaningful, and why
# ---------------------------------------------------------------------------------------------

# Substantive tokens worth surfacing in a change report: number+unit spans ("30-day", "24 months"),
# dollar amounts, spelled-out or bare-year dates, form numbers (I-765, I-983, I-129, I-20, ...), and
# a short list of rule-shaped keywords. Deliberately broad and approximate -- see `highlights`'s own
# docstring in ChangeVerdict for why this must never influence `status`.
_HIGHLIGHT_RE = re.compile(
    r"""
    \d+[\s-]?(?:day|days|month|months|year|years)\b
    | \$\d[\d,]*(?:\.\d+)?
    | \b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},\s*\d{4}\b
    | \b(?:19|20)\d{2}\b
    | \bI-\d{2,4}[A-Z]?\b
    | \b(?:must|may|eligible|required|deadline|effective|extension)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_AGGRESSIVE_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _aggressively_normalize(line: str) -> str:
    """Lowercase and strip every non-alphanumeric character -- used only to test whether two lines
    are "the same words, different formatting" (step 4 of classify_change), never to decide
    anything about content by itself.
    """
    return _AGGRESSIVE_STRIP_RE.sub("", line.lower())


def _extract_highlights(lines: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for line in lines:
        for match in _HIGHLIGHT_RE.finditer(line):
            token = match.group(0)
            key = token.lower()
            if key not in seen:
                seen.add(key)
                found.append(token)
    return found


@dataclass
class ChangeVerdict:
    """The result of comparing an old and a new snapshot body.

    `highlights` is EXPLANATORY ONLY: substantive tokens (numbers with units, dollar amounts,
    dates, form numbers, rule-shaped keywords) spotted in the lines that actually differ, so a
    human reading the refresh report can tell at a glance what kind of thing changed without
    reading the full diff. It is computed from the same added/removed lines `status` was already
    decided from, but it never feeds back into that decision -- see `classify_change`'s own
    docstring for the exact rule, which never mentions `highlights` at all. A change with a real
    rule edit but no token this list recognizes still classifies `meaningful`; a change loaded with
    highlight-looking tokens in an otherwise-cosmetic reformatting still classifies `cosmetic`.
    """

    status: str  # "unchanged" | "cosmetic" | "meaningful"
    reason: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added_count: int = 0
    removed_count: int = 0
    highlights: list[str] = field(default_factory=list)


def classify_change(old_body: str, new_body: str) -> ChangeVerdict:
    """Classify a source's re-fetched body against what is currently indexed.

    Exactly these five steps, in this order:

    1. Normalize both sides with `normalize_for_diff`.
    2. If the normalized line lists are identical -> "unchanged" ("identical_after_normalization").
    3. Otherwise, if they hold the same multiset of lines in a different order -> "cosmetic"
       ("reordered_only").
    4. Otherwise, take the symmetric difference (the lines only the old side has, and the lines
       only the new side has). Aggressively normalize each differing line (lowercase, strip every
       non-alphanumeric character). If the aggressively-normalized multisets of added and removed
       lines now match each other -> "cosmetic" ("formatting_only").
    5. Otherwise -> "meaningful" ("content_lines_changed").

    The bias is deliberate: any real add or removal of a non-boilerplate line is "meaningful". A
    needless re-index is cheap; serving a stale rule to someone is not.

    Known failure modes, stated honestly rather than mitigated:

    - A real rule change made only inside a line `normalize_for_diff`'s boilerplate filter drops
      (for example, if a rule's number ever appeared only in a "Last updated: ..." style line, which
      none of this corpus's real rule text does today) would read as unchanged. This is the UNSAFE
      direction, and it is not mitigated here.
    - A typo fix or a re-worded sentence with no rule change reads as "meaningful" and triggers a
      needless re-index, because it fails step 3 (order differs, not just content) and often fails
      step 4 too (the words themselves changed, not just their formatting). This is the safe
      direction: wasted work, never a stale answer.
    - The comparison is line-based, so re-flowing one paragraph into different line breaks (with
      the exact same words) reads as "meaningful", because it changes which literal lines exist to
      compare, even though no word actually changed.
    - This detects "the text we index changed", not "the rule changed". It cannot tell those apart,
      and does not try to.
    """
    old_lines = normalize_for_diff(old_body)
    new_lines = normalize_for_diff(new_body)

    if old_lines == new_lines:
        return ChangeVerdict(status="unchanged", reason="identical_after_normalization")

    old_counts = Counter(old_lines)
    new_counts = Counter(new_lines)

    if old_counts == new_counts:
        return ChangeVerdict(status="cosmetic", reason="reordered_only")

    added_counts = new_counts - old_counts
    removed_counts = old_counts - new_counts
    added_lines = list(added_counts.elements())
    removed_lines = list(removed_counts.elements())
    highlights = _extract_highlights(added_lines + removed_lines)

    aggressive_added = Counter(_aggressively_normalize(line) for line in added_lines)
    aggressive_removed = Counter(_aggressively_normalize(line) for line in removed_lines)

    status = "cosmetic" if aggressive_added == aggressive_removed else "meaningful"
    reason = "formatting_only" if status == "cosmetic" else "content_lines_changed"

    return ChangeVerdict(
        status=status,
        reason=reason,
        added=added_lines[:20],
        removed=removed_lines[:20],
        added_count=len(added_lines),
        removed_count=len(removed_lines),
        highlights=highlights,
    )


def _frontmatter_date_iso(frontmatter: dict, key: str) -> str | None:
    parsed = _parse_iso_date(frontmatter.get(key))
    return parsed.isoformat() if parsed else None


# ---------------------------------------------------------------------------------------------
# DB bookkeeping. Importable without langgraph; each function takes a connection directly.
#
# WHY THE SPLIT: an unchanged or cosmetic re-crawl updates `last_verified_at` ONLY (via
# touch_last_verified) -- this source was checked, right now, and nothing worth re-indexing
# changed. A meaningful change updates `fetched_at` and `page_last_updated` too, and re-indexes
# (via reindex_source). This is what keeps `fetched_at` honest: on a cosmetic change we
# deliberately do NOT re-index, so the rows still hold exactly the content that was downloaded at
# the older `fetched_at` -- the column keeps its Phase 0 meaning, "when the content in this row was
# downloaded", rather than drifting to mean "when we last looked at this source".
# ---------------------------------------------------------------------------------------------


async def touch_last_verified(
    conn: psycopg.AsyncConnection,
    source_url: str,
    *,
    now: datetime,
    rule_effective_date: date | None,
) -> int:
    """Bookkeeping only: moves `last_verified_at` and syncs `rule_effective_date`. Never touches
    `fetched_at`, `page_last_updated`, `content`, or `embedding` -- see the WHY THE SPLIT comment
    above. `rule_effective_date` is synced here (even though nothing else changed) because it is a
    curator annotation carried in the snapshot's own frontmatter, not fetched page content, so
    keeping the table's copy current is bookkeeping in exactly the sense `last_verified_at` is.
    Returns the number of rows updated.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE documents SET last_verified_at = %s, rule_effective_date = %s "
                "WHERE source_url = %s",
                (now, rule_effective_date, source_url),
            )
            return cur.rowcount


async def reindex_source(
    conn: psycopg.AsyncConnection,
    embedder: Embedder,
    *,
    source_url: str,
    resolved_url: str | None,
    page_last_updated: date | None,
    rule_effective_date: date | None,
    chunks: list[dict],
    now: datetime,
) -> int:
    """A meaningful change: delete this source's old rows and insert freshly embedded chunks, all
    in one transaction, setting `fetched_at = last_verified_at = now` (this source was both
    re-fetched and re-checked right now) plus the new `page_last_updated`/`rule_effective_date`.

    Reuses app.ingest._embed_and_store instead of forking a second copy of the INSERT -- the same
    delete-then-insert-in-one-transaction shape `python -m app.ingest` itself uses. Returns the
    number of chunks written.
    """
    await _embed_and_store(
        conn,
        embedder,
        source_url=source_url,
        resolved_url=resolved_url,
        page_last_updated=page_last_updated,
        rule_effective_date=rule_effective_date,
        chunks=chunks,
        now=now,
    )
    return len(chunks)


def make_conn_factory(
    database_url: str,
) -> Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection]]:
    """A zero-arg callable returning a fresh connection (pgvector types registered) as an async
    context manager that closes the connection on exit. A new connection per node call is simple
    and cheap enough for this job's low-frequency, per-source writes -- there is no long-lived
    connection pool here the way app/db.py's `hybrid_search` has one, because this job is a
    once-a-day batch process, not a request-serving one. Tests supply their own conn_factory
    pointed at a scratch database instead of calling this.
    """

    @asynccontextmanager
    async def _factory():
        conn = await psycopg.AsyncConnection.connect(database_url)
        await register_vector_async(conn)
        try:
            yield conn
        finally:
            await conn.close()

    return _factory


# ---------------------------------------------------------------------------------------------
# The graph. No langgraph import above this line in the module -- see the module docstring.
# ---------------------------------------------------------------------------------------------


class RefreshState(TypedDict):
    source_url: str
    topic: str
    title: str | None
    run_id: str
    attempts: int
    max_attempts: int
    status: str  # "pending" initially; terminal: unchanged | cosmetic | meaningful | fetch_failed
    reason: str | None
    error: str | None
    resolved_url: str | None
    page_last_updated: str | None  # ISO date string, or None
    rule_effective_date: str | None  # ISO date string, or None
    body: str | None
    verdict: dict | None  # a serialized ChangeVerdict (dataclasses.asdict)
    chunks: list[dict]  # chunk_markdown's output; internal plumbing between chunk -> embed/reindex
    chunks_indexed: int
    fetched_this_run: bool
    node_trail: list[str]


@dataclass
class RefreshDeps:
    """Everything a node needs beyond the graph state itself, all injectable so tests can drive
    the graph with no network and no real database: a fake `fetcher` simulates a changed source, a
    fetch failure, or a kill; a `conn_factory` pointed at a scratch database; a fake or stub
    `embedder`.
    """

    fetcher: Callable[[dict], Awaitable[FetchedPage | None]]
    conn_factory: Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection]]
    embedder: Embedder
    raw_dir: Path
    backoff_seconds: float
    dry_run: bool


def _initial_state(entry: dict, run_id: str, max_attempts: int) -> RefreshState:
    return {
        "source_url": entry["url"],
        "topic": entry["topic"],
        "title": entry.get("title"),
        "run_id": run_id,
        "attempts": 0,
        "max_attempts": max_attempts,
        "status": "pending",
        "reason": None,
        "error": None,
        "resolved_url": None,
        "page_last_updated": None,
        "rule_effective_date": None,
        "body": None,
        "verdict": None,
        "chunks": [],
        "chunks_indexed": 0,
        "fetched_this_run": False,
        "node_trail": [],
    }


async def _fetch_node(deps: RefreshDeps, state: RefreshState) -> dict:
    """Fetch this source. Increments `attempts`; on any failure (the fetcher raised, or returned
    None for a robots.txt disallow) sleeps `deps.backoff_seconds` -- a bounded backoff, never an
    unbounded retry, since `_after_fetch` below stops retrying once `attempts >= max_attempts`.
    """
    attempts = state["attempts"] + 1
    entry = {"url": state["source_url"], "topic": state["topic"], "title": state.get("title")}
    trail = [*state["node_trail"], "fetch"]

    try:
        fetched = await deps.fetcher(entry)
    except Exception as exc:  # noqa: BLE001 -- recorded on state; _after_fetch decides retry/fail
        if deps.backoff_seconds:
            await asyncio.sleep(deps.backoff_seconds)
        return {
            "attempts": attempts,
            "error": f"{type(exc).__name__}: {exc}",
            "body": None,
            "node_trail": trail,
        }

    if fetched is None:
        if deps.backoff_seconds:
            await asyncio.sleep(deps.backoff_seconds)
        return {
            "attempts": attempts,
            "error": "robots_disallowed",
            "body": None,
            "node_trail": trail,
        }

    return {
        "attempts": attempts,
        "error": None,
        "body": fetched.body_markdown,
        "resolved_url": fetched.resolved_url,
        "title": fetched.title,
        "page_last_updated": (
            fetched.page_last_updated.isoformat() if fetched.page_last_updated else None
        ),
        "fetched_this_run": True,
        "node_trail": trail,
    }


def _after_fetch(state: RefreshState) -> str:
    if state.get("body") is not None:
        return "diff"
    if state["attempts"] < state["max_attempts"]:
        return "fetch"
    return "record_failure"


async def _diff_node(deps: RefreshDeps, state: RefreshState) -> dict:
    """Compare the freshly fetched body against whatever snapshot currently sits on disk for this
    source (always read from the REAL raw_dir, dry-run or not -- dry-run only forbids writes).
    Also carries forward `rule_effective_date` from the existing snapshot's frontmatter: it is a
    curator annotation, not something re-derived from the fetched page.
    """
    trail = [*state["node_trail"], "diff"]
    existing_index = load_existing_snapshot_index(deps.raw_dir)
    existing_path = existing_index.get(state["source_url"])

    if existing_path is None:
        old_frontmatter: dict = {}
        old_body: str | None = None
    else:
        old_frontmatter, old_body = load_snapshot(existing_path)

    rule_effective_date = _frontmatter_date_iso(old_frontmatter, "rule_effective_date")

    if old_body is None:
        # No existing snapshot at all for this source (a manifest entry ingest.py never saw) --
        # there is nothing to diff against, so it is treated as a full first-time index.
        verdict = ChangeVerdict(status="meaningful", reason="no_existing_snapshot")
    else:
        verdict = classify_change(old_body, state["body"])

    return {
        "verdict": asdict(verdict),
        "rule_effective_date": rule_effective_date,
        "node_trail": trail,
    }


def _after_diff(state: RefreshState) -> str:
    return "chunk" if state["verdict"]["status"] == "meaningful" else "verify_only"


async def _chunk_node(deps: RefreshDeps, state: RefreshState) -> dict:
    """Meaningful-change path, part 1: rewrite the snapshot on disk (unless dry-run) with the
    freshly fetched body, preserving unknown frontmatter keys via build_frontmatter exactly the way
    `python -m app.ingest` does -- so the snapshot on disk is always the content that is actually
    indexed, and a subsequent run's diff compares against what was really last indexed. Then chunk
    that same body. On unchanged/cosmetic, this node never runs at all, so the snapshot is left
    untouched -- meaning a cosmetic difference is re-detected as cosmetic on every subsequent run,
    which is correct: "unchanged" then means "identical to what we indexed".
    """
    trail = [*state["node_trail"], "chunk"]
    source_url = state["source_url"]
    topic = state["topic"]
    body = state["body"]

    existing_index = load_existing_snapshot_index(deps.raw_dir)
    snapshot_path = existing_index.get(source_url) or mint_snapshot_filename(
        topic, source_url, deps.raw_dir
    )
    existing_frontmatter: dict = {}
    if snapshot_path.exists():
        existing_frontmatter, _ = load_snapshot(snapshot_path)

    computed_frontmatter = {
        "source_url": source_url,
        "resolved_url": state.get("resolved_url"),
        "title": state.get("title"),
        "fetched_at": date.today().isoformat(),
        "page_last_updated": state.get("page_last_updated"),
        "topic": topic,
    }
    frontmatter = build_frontmatter(existing_frontmatter, computed_frontmatter)

    if not deps.dry_run:
        deps.raw_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(render_snapshot(frontmatter, body), encoding="utf-8")

    chunks = chunk_markdown(body)
    return {"chunks": chunks, "node_trail": trail}


async def _embed_node(deps: RefreshDeps, state: RefreshState) -> dict:
    """A pass-through, kept as its own graph node purely for state-machine/observability clarity.
    The real embedding call happens exactly once, inside reindex_source's call to
    app.ingest._embed_and_store, which computes vectors and writes rows together in one
    transaction -- reusing the INSERT shape rather than forking a second copy of it (see the
    module's WHY THE SPLIT comment). Embedding here too would mean embedding every chunk twice for
    no benefit.
    """
    del deps
    return {"node_trail": [*state["node_trail"], "embed"]}


async def _reindex_node(deps: RefreshDeps, state: RefreshState) -> dict:
    trail = [*state["node_trail"], "reindex"]
    chunks = state["chunks"]
    verdict = state["verdict"]

    if deps.dry_run:
        return {
            "status": verdict["status"],
            "reason": verdict["reason"],
            "chunks_indexed": len(chunks),
            "node_trail": trail,
        }

    now = datetime.now(UTC)
    page_last_updated = _parse_iso_date(state.get("page_last_updated"))
    rule_effective_date = _parse_iso_date(state.get("rule_effective_date"))

    async with deps.conn_factory() as conn:
        count = await reindex_source(
            conn,
            deps.embedder,
            source_url=state["source_url"],
            resolved_url=state.get("resolved_url"),
            page_last_updated=page_last_updated,
            rule_effective_date=rule_effective_date,
            chunks=chunks,
            now=now,
        )

    return {
        "status": verdict["status"],
        "reason": verdict["reason"],
        "chunks_indexed": count,
        "node_trail": trail,
    }


async def _verify_only_node(deps: RefreshDeps, state: RefreshState) -> dict:
    """Unchanged/cosmetic path: bookkeeping only, via touch_last_verified. Never writes a snapshot,
    never re-indexes.
    """
    trail = [*state["node_trail"], "verify_only"]
    verdict = state["verdict"]

    if not deps.dry_run:
        now = datetime.now(UTC)
        rule_effective_date = _parse_iso_date(state.get("rule_effective_date"))
        async with deps.conn_factory() as conn:
            await touch_last_verified(
                conn, state["source_url"], now=now, rule_effective_date=rule_effective_date
            )

    return {
        "status": verdict["status"],
        "reason": verdict["reason"],
        "node_trail": trail,
    }


async def _record_failure_node(deps: RefreshDeps, state: RefreshState) -> dict:
    del deps
    trail = [*state["node_trail"], "record_failure"]
    return {
        "status": "fetch_failed",
        "reason": state.get("error") or "max_attempts_exceeded",
        "node_trail": trail,
    }


def build_refresh_graph(deps: RefreshDeps):
    """Build (but do not compile) the refresh StateGraph for `deps`.

    START -> fetch -> [diff | fetch (retry) | record_failure]
    diff -> [chunk (meaningful) | verify_only (unchanged/cosmetic)]
    chunk -> embed -> reindex -> END
    verify_only -> END
    record_failure -> END

    Imports langgraph lazily, here rather than at module scope -- see the module docstring for why.
    """
    from langgraph.graph import END, START, StateGraph

    async def fetch_node(state: RefreshState) -> dict:
        return await _fetch_node(deps, state)

    async def diff_node(state: RefreshState) -> dict:
        return await _diff_node(deps, state)

    async def chunk_node(state: RefreshState) -> dict:
        return await _chunk_node(deps, state)

    async def embed_node(state: RefreshState) -> dict:
        return await _embed_node(deps, state)

    async def reindex_node(state: RefreshState) -> dict:
        return await _reindex_node(deps, state)

    async def verify_only_node(state: RefreshState) -> dict:
        return await _verify_only_node(deps, state)

    async def record_failure_node(state: RefreshState) -> dict:
        return await _record_failure_node(deps, state)

    graph = StateGraph(RefreshState)
    graph.add_node("fetch", fetch_node)
    graph.add_node("diff", diff_node)
    graph.add_node("chunk", chunk_node)
    graph.add_node("embed", embed_node)
    graph.add_node("reindex", reindex_node)
    graph.add_node("verify_only", verify_only_node)
    graph.add_node("record_failure", record_failure_node)

    graph.add_edge(START, "fetch")
    graph.add_conditional_edges(
        "fetch",
        _after_fetch,
        {"diff": "diff", "fetch": "fetch", "record_failure": "record_failure"},
    )
    graph.add_conditional_edges(
        "diff", _after_diff, {"chunk": "chunk", "verify_only": "verify_only"}
    )
    graph.add_edge("chunk", "embed")
    graph.add_edge("embed", "reindex")
    graph.add_edge("reindex", END)
    graph.add_edge("verify_only", END)
    graph.add_edge("record_failure", END)
    return graph


# ---------------------------------------------------------------------------------------------
# The driver and the report
# ---------------------------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset({"unchanged", "cosmetic", "meaningful", "fetch_failed"})


def _verdict_evidence(verdict: dict | None) -> dict:
    """Pull the evidence `ChangeVerdict` already computed -- the sample added/removed lines
    (already capped at 20 by `classify_change`), their counts, and `highlights` -- out of the
    graph's serialized verdict dict, so both `SourceResult` construction sites (the fresh-run path
    and the already-terminal/resumed path in `_drive`) share one place that decides how to read a
    verdict dict. Defaults safely to "nothing found" when no verdict exists at all: the
    `fetch_failed` path never reaches `_diff_node`, so `state["verdict"]` is still the initial
    `None`, and the per-source exception handler in `_drive` never builds a verdict dict either.
    """
    if not verdict:
        return {"added": [], "removed": [], "added_count": 0, "removed_count": 0, "highlights": []}
    return {
        "added": verdict.get("added", []),
        "removed": verdict.get("removed", []),
        "added_count": verdict.get("added_count", 0),
        "removed_count": verdict.get("removed_count", 0),
        "highlights": verdict.get("highlights", []),
    }


def _short_source_label(url: str) -> str:
    """A compact, still-identifying label for a `render_table` row: the host plus the last path
    segment (trailing slash stripped). Display-only -- the full URL stays in
    `SourceResult.source_url` and therefore in the `--json` output untouched.
    """
    parsed = urlparse(url)
    last_segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return f"{parsed.netloc}/{last_segment}" if last_segment else parsed.netloc


@dataclass
class SourceResult:
    source_url: str
    status: str
    reason: str | None
    resumed: bool
    fetched_this_run: bool
    chunks_indexed: int
    attempts: int
    node_trail: list[str]
    # Evidence carried from the graph's ChangeVerdict (see _verdict_evidence) -- so both the
    # rendered table and the --json report can show what actually changed, not just that something
    # did. `added`/`removed` are the sample changed lines (already capped at 20 by
    # classify_change); `highlights` stays explanatory only, exactly as ChangeVerdict's own
    # docstring promises -- none of this feeds back into `status`/`reason`.
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added_count: int = 0
    removed_count: int = 0
    highlights: list[str] = field(default_factory=list)


@dataclass
class RefreshReport:
    run_id: str
    results: list[SourceResult]
    http_fetches_this_run: int

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(_TERMINAL_STATUSES, 0)
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        return counts

    def exit_code(self) -> int:
        return 1 if any(r.status == "fetch_failed" for r in self.results) else 0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "http_fetches_this_run": self.http_fetches_this_run,
            "counts": self.counts(),
            "results": [asdict(r) for r in self.results],
        }

    def render_table(self) -> str:
        """Render the human-readable table. The source column is sized to the widest SHORTENED
        label (`_short_source_label`: host + last path segment), not the widest full URL, which is
        what made every row overflow its column before -- several manifest URLs run well past 90
        characters. Every row's `status` token lands at the same column regardless of label length.

        For any source that is not `unchanged`, the row also carries `+added/-removed` line counts
        and `highlights` (if any) as trailing text, so a change is visible at a glance. Sample
        added/removed lines are never printed here -- that is what `--json` is for.
        """
        lines = [f"Refresh run {self.run_id}", ""]
        labels = {r.source_url: _short_source_label(r.source_url) for r in self.results}
        source_width = max([len("source")] + [len(label) for label in labels.values()])
        header = (
            f"{'source':<{source_width}} {'status':<12} {'fetched':<8} {'reason':<28} "
            f"{'chunks':>6}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for r in self.results:
            fetched = "resumed" if r.resumed else ("fetched" if r.fetched_this_run else "-")
            row = (
                f"{labels[r.source_url]:<{source_width}} {r.status:<12} {fetched:<8} "
                f"{(r.reason or ''):<28} {r.chunks_indexed:>6}"
            )
            if r.status != "unchanged":
                row += f"  +{r.added_count}/-{r.removed_count}"
                if r.highlights:
                    row += "  " + ", ".join(r.highlights)
            lines.append(row)
        lines.append("")
        counts = self.counts()
        lines.append("Counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        lines.append(f"HTTP fetches this run: {self.http_fetches_this_run}")
        return "\n".join(lines)


def _today_utc_iso() -> str:
    return datetime.now(UTC).date().isoformat()


async def run_refresh(
    *,
    settings: Settings,
    manifest: list[dict] | None = None,
    raw_dir: str | Path | None = None,
    run_id: str | None = None,
    checkpoint_path: str | None = None,
    only: list[str] | None = None,
    dry_run: bool = False,
    fetcher: Callable[[dict], Awaitable[FetchedPage | None]] | None = None,
    conn_factory: Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection]] | None = None,
    embedder: Embedder | None = None,
    max_attempts: int | None = None,
    backoff_seconds: float | None = None,
) -> RefreshReport:
    """Iterate the manifest, running (or resuming) one refresh graph invocation per source.

    `run_id` defaults to today's UTC date, so a run killed and restarted the same day resumes
    (same thread_id = f"{run_id}:{source_url}", same checkpoint), and tomorrow's cron gets a fresh
    id (a brand-new thread with no checkpoint history at all). `--run-id` on the CLI overrides this.

    For each source: read this thread's checkpointed state (`graph.aget_state`). If it already
    reached a terminal status and has no pending work (`snapshot.next == ()`), SKIP it entirely --
    the graph is never invoked, so the fetcher is never called again for it -- and record it as
    `resumed=True, fetched_this_run=False`. If it has pending work (`snapshot.next != ()`), resume
    with `graph.ainvoke(None, config)`. Otherwise, invoke it fresh.

    One source raising must never stop the others: each iteration is wrapped in its own try/except,
    recording `fetch_failed` and continuing.

    `fetcher`, `conn_factory`, and `embedder` are all overridable so tests can drive this with no
    network and no real database; production (main() below) leaves them unset and this function
    builds real ones (an httpx client + robots/rate-limiter for the default fetcher, a psycopg
    connection factory, and the configured embedder).
    """
    resolved_run_id = run_id or _today_utc_iso()
    raw_dir_path = Path(raw_dir) if raw_dir is not None else Path(settings.RAW_SNAPSHOT_DIR)
    resolved_checkpoint_path = checkpoint_path or settings.REFRESH_CHECKPOINT_PATH
    resolved_max_attempts = (
        max_attempts if max_attempts is not None else settings.REFRESH_MAX_FETCH_ATTEMPTS
    )
    resolved_backoff = (
        backoff_seconds if backoff_seconds is not None else settings.REFRESH_RETRY_BACKOFF_SECONDS
    )

    manifest_entries = (
        manifest if manifest is not None else read_manifest(Path(settings.SOURCES_MANIFEST_PATH))
    )
    if only:
        only_set = set(only)
        manifest_entries = [entry for entry in manifest_entries if entry["url"] in only_set]

    resolved_conn_factory = conn_factory or make_conn_factory(settings.DATABASE_URL)
    resolved_embedder = embedder if embedder is not None else get_embedder(settings)

    fetch_count = {"n": 0}

    async def _counting_fetcher(inner: Callable[[dict], Awaitable], entry: dict):
        fetch_count["n"] += 1
        return await inner(entry)

    results: list[SourceResult] = []

    async def _drive(active_fetcher: Callable[[dict], Awaitable[FetchedPage | None]]) -> None:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        deps = RefreshDeps(
            fetcher=active_fetcher,
            conn_factory=resolved_conn_factory,
            embedder=resolved_embedder,
            raw_dir=raw_dir_path,
            backoff_seconds=resolved_backoff,
            dry_run=dry_run,
        )
        graph_builder = build_refresh_graph(deps)

        async with AsyncSqliteSaver.from_conn_string(str(resolved_checkpoint_path)) as checkpointer:
            graph = graph_builder.compile(checkpointer=checkpointer)

            for entry in manifest_entries:
                source_url = entry["url"]
                config = {"configurable": {"thread_id": f"{resolved_run_id}:{source_url}"}}
                try:
                    snapshot = await graph.aget_state(config)
                    already_terminal = (
                        bool(snapshot.values)
                        and snapshot.next == ()
                        and (snapshot.values.get("status") in _TERMINAL_STATUSES)
                    )
                    if already_terminal:
                        results.append(
                            SourceResult(
                                source_url=source_url,
                                status=snapshot.values["status"],
                                reason=snapshot.values.get("reason"),
                                resumed=True,
                                fetched_this_run=False,
                                chunks_indexed=snapshot.values.get("chunks_indexed", 0),
                                attempts=snapshot.values.get("attempts", 0),
                                node_trail=list(snapshot.values.get("node_trail", [])),
                                **_verdict_evidence(snapshot.values.get("verdict")),
                            )
                        )
                        continue

                    if snapshot.values and snapshot.next != ():
                        final_state = await graph.ainvoke(None, config)
                    else:
                        final_state = await graph.ainvoke(
                            _initial_state(entry, resolved_run_id, resolved_max_attempts), config
                        )

                    results.append(
                        SourceResult(
                            source_url=source_url,
                            status=final_state["status"],
                            reason=final_state.get("reason"),
                            resumed=False,
                            fetched_this_run=final_state.get("fetched_this_run", False),
                            chunks_indexed=final_state.get("chunks_indexed", 0),
                            attempts=final_state.get("attempts", 0),
                            node_trail=list(final_state.get("node_trail", [])),
                            **_verdict_evidence(final_state.get("verdict")),
                        )
                    )
                except (
                    Exception
                ) as exc:  # noqa: BLE001 -- one source's failure must never stop others
                    results.append(
                        SourceResult(
                            source_url=source_url,
                            status="fetch_failed",
                            reason=f"unhandled_error: {type(exc).__name__}: {exc}",
                            resumed=False,
                            fetched_this_run=False,
                            chunks_indexed=0,
                            attempts=0,
                            node_trail=[],
                        )
                    )

    if fetcher is not None:
        await _drive(functools.partial(_counting_fetcher, fetcher))
    else:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": settings.USER_AGENT},
        ) as client:
            robots = RobotsCache(client, settings.USER_AGENT)
            rate_limiter = HostRateLimiter(settings.CRAWL_DELAY_SECONDS)
            default_fetcher = functools.partial(fetch_page, client, robots, rate_limiter)
            await _drive(functools.partial(_counting_fetcher, default_fetcher))

    return RefreshReport(
        run_id=resolved_run_id, results=results, http_fetches_this_run=fetch_count["n"]
    )


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.recrawl",
        description=(
            "Re-crawl the sources manifest, diff each page against its snapshot, and re-index "
            "sources whose content meaningfully changed."
        ),
    )
    parser.add_argument("--run-id", default=None, help="default: today's UTC date")
    parser.add_argument(
        "--checkpoint", default=None, help="default: Settings.REFRESH_CHECKPOINT_PATH"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch, diff, and classify, but make no database writes and no snapshot writes",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="URL",
        help="restrict to one manifest entry (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = get_settings()

    report = asyncio.run(
        run_refresh(
            settings=settings,
            run_id=args.run_id,
            checkpoint_path=args.checkpoint,
            only=args.only,
            dry_run=args.dry_run,
        )
    )

    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render_table())
    sys.exit(report.exit_code())


if __name__ == "__main__":
    main()
