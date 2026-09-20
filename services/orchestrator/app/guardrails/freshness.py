"""Surfacing freshness in an answer: what is currently known about each retrieved source's own
staleness, and any dated rule change one of those sources states.

Pure: no network, no DB, no LangGraph. Given the chunks a query actually retrieved (in retrieval
order), the bracket indices the generated answer actually cited, and today's date, `build_freshness`
produces a structured block (app/schemas.py::Freshness) recording, per distinct retrieved source,
its page_last_updated/fetched_at/last_verified_at and any rule_effective_date it carries, plus one
`FreshnessNotice` per distinct source that carries a `rule_status` OR a `rule_effective_date` -- see
build_freshness's own docstring, and "WHY THAT GATE WAS OVERRIDDEN" below, for why this is no
longer additionally gated on rank or citation. `freshness_notice_text` turns those notices into the
sentence(s) app/pipeline.py appends to the rendered answer.

CURATOR-STATED FORCE (2026-09-19, docs/adr/0023-curator-rule-status.md; see REPORT.md, "The
injunction, and why this is not fixed"). This module used to compare `rule_effective_date` to
`today` with `<=` to decide `in_effect` and to choose "took effect on" vs. "takes effect on"
wording. That comparison is gone. `in_effect` is now `app/rule_status.py::
is_in_force(chunk.rule_status)` -- a curator-stated fact, never arithmetic -- and the wording is
chosen from `rule_status` directly (see `freshness_notice_text` below), covering two states the
date-only version had no way to express at all: `enjoined` (a court has blocked the rule; it may
still take effect later) and `not_in_force` (set aside, vacated, or withdrawn; not coming back).
The date comparison this replaces is exactly what inverted on 2026-09-15, asserting in this
system's own uncited voice that a rule a federal court had enjoined the day before had "taken
effect" -- see docs/adr/0020-temporal-qualification-guard.md's own amendment for the fuller
account.

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

SUPERSEDED, 2026-09-07 red-team fix -- kept below, not deleted, because it explains a real cost the
current (ungated) behavior reintroduces: `build_freshness` no longer gates a notice on rank or
citation at all. EVERY distinct retrieved source that carries a `rule_effective_date` gets a
notice now, regardless of rank or citation. The reasoning that originally justified the gate, and
why it was overridden, both follow.

WHY A NOTICE USED TO BE GATED (not just "carries a dated rule") -- ORIGINAL REASONING, NOW
OVERRIDDEN: the corpus's largest single source is the fixed-admission FAQ (dozens of chunks, all
sharing one rule_effective_date), so it gets retrieved incidentally on questions that have nothing
to do with it -- retrieval returning a chunk from that page is not the same thing as the dated rule
being what the question is actually about. Appending the notice sentence unconditionally, on every
retrieval that happens to include one of that page's chunks, produced answers about unrelated
topics (a STEM OPT training-plan question, a pre-completion-OPT question) that ended with a warning
about an unrelated Sept 15 2026 rule change -- misleading, not merely noisy. Gating on "was this
dated source ranked first" or "did the answer actually cite it" tied the notice to evidence the
retrieval/generation step itself already produced, rather than to a tuned distance threshold: both
are non-arbitrary facts about this specific query's retrieval and generation, not constants fitted
to any particular set of questions.

WHY THAT GATE WAS OVERRIDDEN: red-team verification on 2026-09-07 measured the gate actually firing
in production, on the real live stack, not merely in theory. Six real phrasings of "how long do I
have to leave the US after my program ends" were each run six times. On five of those six runs,
three fixed-admission chunks were retrieved (ranks 3-5, listed on the answer's own source list) but
none of them was top-ranked or cited, so `freshness.notices` came back empty and the rendered
answer stated a bare "60 days" with no effective-date qualification at all -- eight days before the
rule that number depends on changes. The gate built to stop one false positive (a spurious warning
on an unrelated question) was suppressing the one signal `app/prompts.py`'s temporal rule needs on
the question it exists for. A model cannot reliably infer "this passage is dated" from unmarked
prose alone -- measured at roughly 1 correct run in 6 -- which is also why `app/prompts.py::
format_context` now marks a dated passage mechanically rather than leaving the model to notice it
unaided (see that module's own docstring). `build_freshness` emits a notice for every distinct
retrieved source carrying a `rule_effective_date` now, unconditionally; the noise cost this
reintroduces (a notice firing on a retrieval that only incidentally touched a dated source) is
measured and reported, never tuned away -- see docs/adr/0015-ungate-freshness-notice.md for the
actual before/after count over eval/golden.jsonl.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

from app.db import RetrievedChunk
from app.rule_status import RuleStatus, is_in_force
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


def source_health_state(
    *,
    consecutive_failures: int,
    status: str,
    last_success_at: datetime | None,
    now: datetime,
    broken_after_failures: int,
    broken_after_no_success_days: int,
) -> Literal["ok", "broken"]:
    """Phase 7: the "broken" verdict GET /sources/status reports per source, alongside (not instead
    of) the crawl-freshness band `sources_freshness_state` above already computes for the whole
    corpus. Pure -- no DB, no clock of its own, no import of app.config -- so the threshold values
    are passed in as plain arguments (Settings.SOURCE_BROKEN_CONSECUTIVE_FAILURES/
    SOURCE_BROKEN_NO_SUCCESS_DAYS) rather than read from settings here, the same shape
    `sources_freshness_state` already follows for `now`. The frontend must never recompute this
    rule, the same way it never recomputes the freshness band: it renders app/main.py's verdict.

    BROKEN when ANY of:
      - consecutive_failures >= broken_after_failures (three daily cron ticks running -- see
        app/config.py's comment on SOURCE_BROKEN_CONSECUTIVE_FAILURES for why three), or
      - status == "robots_disallowed" (permanent by nature: nothing about retrying makes a
        disallowed URL fetchable again, so this is broken on the very first occurrence, with no
        failure-count threshold at all), or
      - last_success_at is more than broken_after_no_success_days old, OR last_success_at is None
        (a source that has never once succeeded is unambiguously broken, not merely "unknown" --
        there is no successful check to measure an age from).

    Otherwise "ok". `consecutive_failures < broken_after_failures` at the exact boundary reads
    "ok" (e.g. 2 failures with a threshold of 3): only reaching the threshold, not approaching it,
    counts as broken.
    """
    if status == "robots_disallowed":
        return "broken"
    if consecutive_failures >= broken_after_failures:
        return "broken"
    if last_success_at is None:
        return "broken"
    age = now - last_success_at
    if age > timedelta(days=broken_after_no_success_days):
        return "broken"
    return "ok"


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

    `FreshnessNotice` (`Freshness.notices`): a DIFFERENT set from `sources` above, but -- as of the
    2026-09-07 red-team fix -- narrower ONLY in that it excludes a source with NEITHER a
    `rule_status` NOR a `rule_effective_date`. It is NO LONGER gated on rank or citation: EVERY
    distinct retrieved source that carries either one gets a notice (see the module docstring's
    "WHY THAT GATE WAS OVERRIDDEN" for why -- the previous top-ranked-or-cited gate suppressed the
    notice on 5 of 6 real runs of the same real question, exactly when the reader needed it most).
    2026-09-19: the skip condition used to be "no `rule_effective_date`" alone; it is now "no
    `rule_status` AND no `rule_effective_date`", because `enjoined`/`not_in_force` can carry a
    status with no date at all (`app/rule_status.py`'s own vocabulary table) and such a source must
    still get a notice -- the old, date-only skip would have silently dropped exactly that case.

    `reason` records which one of three, mutually exclusive, ACCURATE facts holds for this source
    (round 2, 2026-09-08 -- see `app/schemas.py::FreshnessNotice.reason`'s own docstring):
      "top_ranked" -- one of its retrieved chunks sits at position 1 (1-based) in `chunks`, i.e.
          `chunks[0]` -- the single highest-ranked chunk `hybrid_search` returned for this query.
      "cited" -- not top-ranked, but the generated answer's bracket citations named a chunk from
          this source (that chunk's 1-based position in `chunks` appears in `cited_indices`).
      "retrieved" -- neither of the above: the source still gets a notice (the gate above is gone),
          but nothing about rank or citation singles it out.
    When a source satisfies both "top_ranked" and "cited" at once, `reason` reports "top_ranked"
    (checked first below); this is an arbitrary tie-break for an arbitrary case, not a signal either
    caller should read anything into.

    Round 1 (2026-09-07) shipped this with only two `reason` values and a documented imprecision:
    every non-top-ranked source reported "cited" regardless of whether it actually was, because
    `app/schemas.py::FreshnessNotice.reason` had no third value and widening that schema was ruled
    out of that round's scope. Round 2 adds "retrieved" and restores an actual `cited_indices` check
    here, so every value `reason` can carry is now a true statement about the source it describes.
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
        # 2026-09-19: skip only when NEITHER annotation is present -- see this function's own
        # docstring. `chunk.rule_effective_date is None` alone used to be the skip; that silently
        # dropped an `enjoined`/`not_in_force` source with a status but no date at all.
        if chunk.rule_status is None and chunk.rule_effective_date is None:
            continue

        positions = positions_by_url[url]
        top_ranked = 1 in positions
        cited = any(p in cited_indices for p in positions)
        # Red-team fix, 2026-09-07: no gate here anymore -- a notice fires for every distinct
        # source that reaches this point (i.e., carries a rule_status or a rule_effective_date),
        # regardless of rank or citation. Round 2, 2026-09-08: `reason` now reports an accurate
        # third value, "retrieved", for a source that is genuinely neither top-ranked nor cited,
        # rather than defaulting to "cited" for that case (see this function's own docstring).
        if top_ranked:
            reason = "top_ranked"
        elif cited:
            reason = "cited"
        else:
            reason = "retrieved"

        notices.append(
            FreshnessNotice(
                source_url=url,
                rule_effective_date=chunk.rule_effective_date,
                rule_status=chunk.rule_status,
                rule_status_source=chunk.rule_status_source,
                rule_status_source_evidences_status=chunk.rule_status_source_evidences_status,
                # Curator-stated (app/rule_status.py::is_in_force), never a date comparison -- see
                # this module's docstring, "CURATOR-STATED FORCE".
                in_effect=is_in_force(chunk.rule_status),
                reason=reason,
            )
        )

    return Freshness(as_of=today, sources=sources, notices=notices)


def _join_source_links(urls: list[str], *, label: str = "source") -> str:
    """Render 1..N source URLs as markdown links joined in readable prose ("[the source](a)", "[the
    first source](a) and [the second source](b)", ...), never dropping any of them.

    `label` is the noun phrase every link carries ("source" by default; `freshness_notice_text`
    passes "rule as published", "court's order", or "source for that" for the enjoined/not_in_force
    wordings -- see that function's own docstring) -- the ONLY hardcoded noun this module ever
    prints for a link; it never describes what the rule itself says.
    """
    if len(urls) == 1:
        return f"[the {label}]({urls[0]})"

    ordinals = ["first", "second", "third", "fourth", "fifth"]
    labels = [
        f"[the {ordinals[i]} {label}]({url})" if i < len(ordinals) else f"[a {label}]({url})"
        for i, url in enumerate(urls)
    ]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _subject(count: int) -> str:
    if count == 1:
        return "One of the sources above describes"
    return "Some of the sources above describe"


def freshness_notice_text(notices: list[FreshnessNotice], *, today: date) -> str | None:
    """The sentence(s) app/pipeline.py appends to the rendered answer, ONE PER DISTINCT
    (rule_effective_date, rule_status) pair, not one per notice -- two retrieved sources that both
    carry the same status and date (the live case: both fixed_admission snapshots state
    rule_status="enjoined") collapse into a single sentence linking every source that stated it,
    rather than repeating the same sentence once per source. `Freshness.notices` itself stays
    one-per-source (that structured field is meant to be granular); only this rendered prose
    collapses. 2026-09-19: the grouping key used to be `(rule_effective_date, in_effect)`; it is
    now `(rule_effective_date, rule_status)` -- `in_effect` is a derived bit that collapses four
    states into two (see app/schemas.py::FreshnessNotice's own docstring), and grouping on it
    directly would merge, for example, a `scheduled` group with an `enjoined` group that happens to
    share a date, rendering one sentence that is only half-true of either source.

    `today` is `Freshness.as_of` from the SAME call that produced `notices` -- passed explicitly
    (never read from the clock in here) the same way every date-taking function in this codebase
    already is. 2026-09-19: `enjoined`/`not_in_force` no longer state `today` at all -- see below,
    "HONEST NOTICE WORDING"; `in_force` with no `rule_effective_date` is now the only branch that
    still does (`in_force`/`scheduled` with a date state their own date instead, exactly as before).

    Wording is generated entirely from each group's own date and `rule_status` -- never a hardcoded
    description of what the rule changed (e.g. never "60 days to 30"), so this stays correct for any
    future dated or contested rule the corpus picks up, not just the one live today.

    HONEST NOTICE WORDING (2026-09-19, docs/adr/0023-curator-rule-status.md). `enjoined`/
    `not_in_force` link `rule_status_source` (the court order or withdrawal notice a curator gave),
    never the chunk's own `source_url` -- linking the retrieved page would assert the block or
    withdrawal in this system's own uncited voice, which is exactly what `rule_status_source` exists
    to avoid (see app/rule_status.py's own docstring). But a bare "See [the source](url)" link
    ITSELF asserted more than the link supported whenever that URL was the Federal Register notice
    for the rule rather than the court's order: it implied the linked page said what the sentence
    just said, when the page says no such thing. `rule_status_source_evidences_status` -- curator-
    stated, never inferred from the URL (app/rule_status.py::validate_rule_status requires it
    whenever `rule_status_source` is set) -- is what makes three things separate instead of
    conflated: the force claim (attributed to "this site's maintainer" when the evidence does not
    say it), the link's label (never a description of what the linked document is called -- see
    `_join_source_links`'s own docstring), and whether the reader is told the source page is silent
    on it. `not_in_force` deliberately never names a mechanism (vacated vs. withdrawn) even when
    `rule_status_source_evidences_status` is True, so its label stays the neutral "the source for
    that", never "the court's order" -- see app/rule_status.py's own vocabulary table for why
    `not_in_force` carries no mechanism at all. `enjoined` handles a missing `rule_effective_date`
    (allowed by that same vocabulary table) by dropping the "was scheduled to take effect on {date}"
    clause and opening with a date-free sentence instead, for both evidences values.

    Returns None when there is nothing to say (no notices at all).
    """
    if not notices:
        return None

    groups: dict[tuple[date | None, str | None], list[FreshnessNotice]] = {}
    for notice in notices:
        key = (notice.rule_effective_date, notice.rule_status)
        groups.setdefault(key, []).append(notice)

    sentences = []
    for (rule_effective_date, rule_status), group_notices in groups.items():
        source_urls = list(dict.fromkeys(n.source_url for n in group_notices))
        subject = _subject(len(source_urls))

        if rule_status in (RuleStatus.ENJOINED.value, RuleStatus.NOT_IN_FORCE.value):
            # Falls back to the retrieved source_url only if rule_status_source is somehow absent
            # -- app/rule_status.py::validate_rule_status requires it for enjoined/not_in_force at
            # load time, so this is a defensive floor against a legacy/unsynced row, never the
            # intended path; it still links SOMETHING rather than raising on an empty
            # `_join_source_links` list.
            status_source_urls = (
                list(
                    dict.fromkeys(
                        n.rule_status_source for n in group_notices if n.rule_status_source
                    )
                )
                or source_urls
            )
            # A legacy/unsynced row can carry rule_status_source with no
            # rule_status_source_evidences_status at all (see app/schemas.py::FreshnessNotice's own
            # docstring) -- treated as False, the cautious reading, never as True: a link this
            # system cannot confirm actually documents the status must never be captioned as though
            # it does.
            evidences_status = bool(group_notices[0].rule_status_source_evidences_status)
            # The maintainer-attribution clause, shared verbatim by every evidences=False wording
            # below (enjoined and not_in_force alike) -- one string, not a phrase copied twice, so
            # the wording cannot drift between the two the next time either changes.
            maintainer_clause = (
                "That is recorded by this site's maintainer; the page above does not say it."
            )

            if rule_status == RuleStatus.ENJOINED.value:
                if evidences_status:
                    court_order_link = _join_source_links(status_source_urls, label="court's order")
                    if rule_effective_date is not None:
                        sentences.append(
                            f"{subject} a rule that was scheduled to take effect on "
                            f"{_format_date(rule_effective_date)}. A court has blocked it and it "
                            f"is not in force. See {court_order_link}."
                        )
                    else:
                        sentences.append(
                            f"{subject} a rule that has been blocked by a court order and is not "
                            f"in force. See {court_order_link}."
                        )
                else:
                    rule_link = _join_source_links(status_source_urls, label="rule as published")
                    if rule_effective_date is not None:
                        sentences.append(
                            f"{subject} a rule that was scheduled to take effect on "
                            f"{_format_date(rule_effective_date)}. It has since been blocked by a "
                            f"court order and is not in force. {maintainer_clause} See "
                            f"{rule_link}."
                        )
                    else:
                        sentences.append(
                            f"{subject} a rule that has been blocked by a court order and is not "
                            f"in force. {maintainer_clause} See {rule_link}."
                        )
            else:  # not_in_force
                if evidences_status:
                    source_link = _join_source_links(status_source_urls, label="source for that")
                    sentences.append(f"{subject} a rule that is not in force. See {source_link}.")
                else:
                    rule_link = _join_source_links(status_source_urls, label="rule as published")
                    sentences.append(
                        f"{subject} a rule that is not in force. {maintainer_clause} See "
                        f"{rule_link}."
                    )
        elif rule_effective_date is not None:
            # `in_force` or `scheduled` (or a legacy/unsynced row with rule_status None but a date
            # -- see app/schemas.py::FreshnessNotice's own docstring on why that combination can
            # transiently exist), both wordings UNCHANGED from before this fix.
            in_effect = is_in_force(rule_status)
            verb_phrase = "took effect on" if in_effect else "takes effect on"
            sentences.append(
                f"{subject} a rule that {verb_phrase} {_format_date(rule_effective_date)}, so the "
                f"answer differs before and after that date. See {_join_source_links(source_urls)}."
            )
        else:
            # `in_force` with no rule_effective_date at all -- allowed by app/rule_status.py's own
            # vocabulary table (the date is optional for `in_force`), so this must not crash; there
            # is no date to state, so the sentence says only that the rule is current.
            sentences.append(
                f"{subject} a rule that is in force today, {_format_date(today)}. See "
                f"{_join_source_links(source_urls)}."
            )
    return " ".join(sentences)
