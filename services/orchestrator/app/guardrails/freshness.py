"""Surfacing freshness in an answer: what is currently known about each retrieved source's own
staleness, and any dated rule change one of those sources states.

Pure: no network, no DB, no LangGraph. Given the chunks a query actually retrieved (in retrieval
order), the bracket indices the generated answer actually cited, and today's date, `build_freshness`
produces a structured block (app/schemas.py::Freshness) recording, per distinct retrieved source,
its page_last_updated/fetched_at/last_verified_at and any rule_effective_date it carries, plus one
`FreshnessNotice` per distinct source that BOTH carries a rule_effective_date AND qualifies to be
surfaced (see build_freshness's own docstring for the two qualifying conditions).
`freshness_notice_text` turns those notices into the sentence(s) app/pipeline.py appends to the
rendered answer.

CRITICAL SCOPE LIMIT: the appended TEXT fires ONLY on the effective-date condition -- never on "one
of these sources was last verified N days ago" or any other crawl-freshness signal. An
effective-date statement is grounded in the corpus itself (both fixed_admission snapshots state
their own effective date in their own retrievable text), so appending it never adds a claim the
retrieved chunks don't already support. A "last verified three days ago" statement is metadata
about OUR crawl, not something any retrieved chunk says -- putting that in the answer text would
add an unsupported claim that RAGAS's faithfulness metric would score against, and that
app/guardrails/citations.py's programmatic check cannot see at all (it never carries a bracket).
Verification/fetch dates are exposed only in the structured `Freshness.sources` field, never in the
answer's prose.

WHY A NOTICE ALSO HAS TO BE GATED (not just "carries a dated rule"): the corpus's largest single
source is the fixed-admission FAQ (dozens of chunks, all sharing one rule_effective_date), so it
gets retrieved incidentally on questions that have nothing to do with it -- retrieval returning a
chunk from that page is not the same thing as the dated rule being what the question is actually
about. Appending the notice sentence unconditionally, on every retrieval that happens to include
one of that page's chunks, produced answers about unrelated topics (a STEM OPT training-plan
question, a pre-completion-OPT question) that ended with a warning about an unrelated Sept 15 2026
rule change -- misleading, not merely noisy. Gating on "was this dated source ranked first" or
"did the answer actually cite it" ties the notice to evidence the retrieval/generation step itself
already produced, rather than to a tuned distance threshold: both are non-arbitrary facts about
this specific query's retrieval and generation, not constants fitted to any particular set of
questions.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from app.db import RetrievedChunk
from app.schemas import Freshness, FreshnessNotice, FreshnessSource


def sources_freshness_state(
    oldest_verified_at: datetime | None, now: datetime
) -> tuple[Literal["current", "recent", "stale", "unknown"], float | None]:
    """Phase 6: the header's "live sources" trust indicator (GET /sources/status). Pure -- no DB,
    no clock of its own -- so the band decision lives here, testable in pytest, rather than in the
    frontend: the frontend must only render what this function decides, never recompute a
    freshness claim from raw dates on its own.

    `oldest_verified_at` is the WEAKEST link: the minimum `last_verified_at` across every distinct
    source in `documents` (app/main.py computes it as `min(per-source min(last_verified_at))`), not
    the newest. A single freshly re-crawled source must never let the indicator claim "current"
    while other sources sat unchecked for a week -- that is the whole reason this endpoint changed
    from `max(last_verified_at)` to a per-source aggregation.

    Bands, by age of that weakest link:
        oldest_verified_at is None   -> ("unknown", None)   -- no rows in `documents` at all
        age_hours <= 24               -> ("current", age_hours)
        age_hours <= 168 (7 days)     -> ("recent",  age_hours)
        otherwise                     -> ("stale",   age_hours)

    A negative age_hours (oldest_verified_at in the future relative to `now` -- clock skew between
    the app process and whatever wrote that timestamp, not a real staleness signal) falls into the
    same `<= 24` branch as any other sub-24-hour value, so it reads as "current" with no special
    case needed and no possibility of raising: there is nothing stale about a timestamp that, from
    this process's point of view, has not happened yet.
    """
    if oldest_verified_at is None:
        return "unknown", None
    age_hours = (now - oldest_verified_at).total_seconds() / 3600
    if age_hours <= 24:
        return "current", age_hours
    if age_hours <= 168:
        return "recent", age_hours
    return "stale", age_hours


def _format_date(value: date) -> str:
    # "%-d" (no leading zero) is not portable across platforms; this is, and reads the same way:
    # "September 15, 2026" rather than "September 05, 2026".
    return f"{value:%B} {value.day}, {value.year}"


def build_freshness(
    chunks: list[RetrievedChunk], *, today: date, cited_indices: set[int]
) -> Freshness:
    """`FreshnessSource` (`Freshness.sources`): one per distinct retrieved source (by citation URL
    -- resolved_url when present, source_url otherwise, matching app/pipeline.py::_citation_url),
    ALWAYS, regardless of rank or citation -- this is the complete freshness bookkeeping for every
    source the query retrieved.

    `FreshnessNotice` (`Freshness.notices`): a DIFFERENT, narrower set. A distinct source qualifies
    for a notice only when it carries a `rule_effective_date` AND at least one of:

      (a) "top_ranked" -- one of its retrieved chunks sits at position 1 (1-based) in `chunks`,
          i.e. `chunks[0]` -- the single highest-ranked chunk `hybrid_search` returned for this
          query, or
      (b) "cited" -- the generated answer actually cited one of its retrieved chunks: that chunk's
          1-based position in `chunks` appears in `cited_indices` (positions line up with the
          bracket numbering app/prompts.py::format_context gave the model, exactly the numbering
          app/guardrails/citations.py::parse_cited_indices reads back out of the generated text).

    Condition (a) exists specifically because citation alone is not enough: a real Phase 5 case
    ("How long do I have to leave the United States after my OPT ends?") has the dated source
    retrieved at rank 1 while the generated answer cites a DIFFERENT chunk for the (still-current)
    60-day rule and never cites the dated one -- exactly the case where the reader most needs the
    warning that a replacement rule exists, and a cite-only gate would drop it. Condition (b) exists
    because top-rank alone is not enough either: a dated source can rank second or later yet still
    be the chunk the answer is actually built from.

    A dated source retrieved but qualifying for NEITHER condition is deliberately left OUT of
    `notices` -- notices are the things worth surfacing in prose, and `sources` above already
    records that source's own `rule_effective_date` regardless, so nothing is lost, only the
    unwarranted prose warning is. When a source qualifies via both conditions at once, `reason` is
    reported as `"top_ranked"` (condition (a) is checked first below); this is an arbitrary
    tie-break for an arbitrary case, not a signal either caller should read anything into.
    """
    sources: list[FreshnessSource] = []
    notices: list[FreshnessNotice] = []
    seen: set[str] = set()
    positions_by_url: dict[str, list[int]] = {}
    for position, chunk in enumerate(chunks, start=1):
        url = chunk.resolved_url or chunk.source_url
        positions_by_url.setdefault(url, []).append(position)

    for chunk in chunks:
        url = chunk.resolved_url or chunk.source_url
        if url in seen:
            continue
        seen.add(url)

        sources.append(
            FreshnessSource(
                source_url=url,
                page_last_updated=chunk.page_last_updated,
                last_verified_at=chunk.last_verified_at,
                fetched_at=chunk.fetched_at,
                rule_effective_date=chunk.rule_effective_date,
            )
        )
        if chunk.rule_effective_date is None:
            continue

        positions = positions_by_url[url]
        top_ranked = 1 in positions
        cited = any(p in cited_indices for p in positions)
        if not (top_ranked or cited):
            continue

        notices.append(
            FreshnessNotice(
                source_url=url,
                rule_effective_date=chunk.rule_effective_date,
                in_effect=chunk.rule_effective_date <= today,
                reason="top_ranked" if top_ranked else "cited",
            )
        )

    return Freshness(as_of=today, sources=sources, notices=notices)


def _join_source_links(urls: list[str]) -> str:
    """Render 1..N source URLs as markdown links joined in readable prose ("[the source](a)", "[the
    first source](a) and [the second source](b)", ...), never dropping any of them.
    """
    if len(urls) == 1:
        return f"[the source]({urls[0]})"

    ordinals = ["first", "second", "third", "fourth", "fifth"]
    labels = [
        f"[the {ordinals[i]} source]({url})" if i < len(ordinals) else f"[a source]({url})"
        for i, url in enumerate(urls)
    ]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def freshness_notice_text(notices: list[FreshnessNotice]) -> str | None:
    """The sentence(s) app/pipeline.py appends to the rendered answer, ONE PER DISTINCT
    (rule_effective_date, in_effect) pair, not one per notice -- two retrieved sources that both
    carry the same dated rule (the live case: both fixed_admission snapshots state
    rule_effective_date=2026-09-15) collapse into a single sentence linking every source that
    stated it, rather than repeating the same sentence once per source. `Freshness.notices` itself
    stays one-per-source (that structured field is meant to be granular); only this rendered prose
    collapses.

    Wording is generated entirely from each group's own date and in_effect flag -- never a
    hardcoded description of what the rule changed (e.g. never "60 days to 30"), so this stays
    correct for any future dated rule the corpus picks up, not just the one live today. Returns
    None when there is nothing to say (no notices at all).
    """
    if not notices:
        return None

    groups: dict[tuple[date, bool], list[str]] = {}
    for notice in notices:
        key = (notice.rule_effective_date, notice.in_effect)
        urls = groups.setdefault(key, [])
        if notice.source_url not in urls:
            urls.append(notice.source_url)

    sentences = []
    for (rule_effective_date, in_effect), urls in groups.items():
        date_str = _format_date(rule_effective_date)
        verb_phrase = "took effect on" if in_effect else "takes effect on"
        subject = (
            "One of the sources above describes"
            if len(urls) == 1
            else "Some of the sources above describe"
        )
        sentences.append(
            f"{subject} a rule that {verb_phrase} {date_str}, so the answer differs before and "
            f"after that date. See {_join_source_links(urls)}."
        )
    return " ".join(sentences)
