"""Programmatic guard qualifying a future-dated rule's figure at the SENTENCE it appears in, not
only in a freshness notice appended once, at the very end of the whole answer.

THE DEFECT, MEASURED. When the corpus holds a current rule and a future-dated replacement, the
generator states the replacement's figure in a sentence carrying no date at all -- "The departure
period is now 30 days" -- while the only date-qualified text sits in the freshness notice
app/pipeline.py step 8 already appends to the very end of `answer_text`
(app/guardrails/freshness.py::freshness_notice_text). The reader's eye is on the claim, not the
trailing sentence eight sentences later.

CRITICAL FACT THIS MODULE IS BUILT AROUND: by the time step 8 runs, `answer_text` ALREADY carries
that freshness notice's own effective-date sentence for every ANSWER and REFUSAL_ADVICE response
(see app/pipeline.py's own comment at this module's call site). A check of the shape "does
`answer_text` contain the effective date anywhere" therefore passes on every such input, whether or
not the SENTENCE actually making the claim carries it -- and detects nothing. This module is
deliberately SENTENCE-scoped: it asks whether the sentence stating the figure carries the date, not
whether the date appears somewhere in the rendered text at all. This is also why app/pipeline.py
calls this BEFORE the freshness notice is appended (see that call site's own comment) -- so the
notice's own date-bearing text is never mistaken, by this module, for generated prose that actually
states the claim.

ALGORITHM.

1. A numeric FIGURE is FUTURE-ONLY if it appears in at least one retrieved chunk whose
   `rule_effective_date` is strictly after `today`, and appears in NO retrieved chunk that is
   undated or dated on or before `today`. A figure appearing in both a future-dated chunk and an
   undated/past-dated chunk is not future-only, and must never fire -- that is exactly the shape of
   figure this module has no basis for qualifying (it is either unchanged by the dated rule, or the
   model could equally have drawn it from a stale or a current source).
2. Figures are extracted with `_extract_figures` below, which excludes: a digit inside a citation
   bracket (`[7]`, `[1, 3]`); a form number (`I-765`, `I-20`, `I-983`, `I-94`, and generally any
   `<letter>-<digits><optional letter>` token, which also excludes a visa category like `H-1B`); a
   bare four-digit year (1900-2100); a digit that is part of a rendered date phrase ("September 15,
   2026", or the corpus's own abbreviated "Sept. 15, 2026" -- both the day and the year); a digit
   inside a URL (`https://i94.cbp.dhs.gov/home`, where "94" is a hostname digit, not a form number
   or a figure -- measured on chunks 675/676); and a digit inside a numeric date (`01/30/2026`, the
   `Last Reviewed/Updated:` footer ingestion preserves verbatim -- measured on chunk 444 and,
   corpus-wide, on chunks 439, 492, 515, 600, and 660; see `_NUMERIC_DATE_RE`). None of these is "a
   rule's figure" in the sense this guard cares about.
3. `answer_text` is split into sentences, kept together with their original separating whitespace,
   so reconstruction is byte-exact for every sentence this module does not touch.
   `_split_sentences_with_separators` also re-merges a split that landed right after one of this
   module's own recognized month abbreviations ("Sept.") and right before a day number, so a date
   like "Sept. 15, 2026" is never torn apart into two "sentences" by its own abbreviating period --
   see that function's own docstring.
4. A sentence fires when it contains at least one future-only figure AND does not already STATE the
   effective date of the future-dated chunk(s) that figure came from, checked by
   `_date_stated_pattern` below against every plausible rendering of that date -- full or
   abbreviated month (each with an optional trailing period), the day with or without a leading
   zero, and an optional comma before the year -- not only the one literal string `_format_date`
   produces. This has to be broader than a literal `_format_date` containment test: the corpus
   itself renders every dated chunk's own date in abbreviated form ("Sept. 15, 2026"), so a model
   answer that copies the corpus's own wording would never satisfy a check for the literal
   "September 15, 2026" -- and the guard would insert a redundant qualifier next to a sentence that
   already carries the date. When a sentence's future-only figures trace back to more than one
   future date, the earliest one not already stated in the sentence is used -- one inserted sentence
   per firing sentence, never one per figure or one per date.
5. Exactly ONE derived sentence is inserted immediately after a firing sentence. Its wording is
   derived from the date and `today` ONLY -- never the figure, the topic, a URL, or any description
   of what the rule says, the same discipline `app/guardrails/freshness.py::freshness_notice_text`
   and `app/prompts.py::_rule_date_note` already follow:

       That figure comes from a rule that takes effect on September 15, 2026. It is not the rule in
       force today, September 11, 2026.

BLOCK VS INSERT (2026-09-12): a firing sentence does not always just get that inserted sentence.
Ten production runs of "What is the grace period after OPT ends?" on 2026-09-12, read by their
opening sentence: 3/10 stated the current rule correctly, 5/10 stated the FUTURE rule as though it
were already current ("The current grace period after post-completion OPT ... ends is 30 days",
three days before that becomes true), 2/10 neither. This guard fired on exactly those five and none
of the other five -- the detection signal above is reliable -- but appending a correction two
sentences later does not unsay a false figure asserted as current in the OPENING sentence, for a
reader skimming it. An honest non-answer naming the source is better than that.

The split: a firing sentence whose OWN figures (`_extract_figures` on the SENTENCE, the same
extractor `_future_only_figures` above uses on chunk content) include at least one figure that ALSO
appears in a CURRENT chunk (`rule_effective_date` is `None` or on/before `today`) still gets the
single derived sentence inserted, exactly as before this split existed: both the current and the
future rule are present in the sentence, only the date placement is wrong -- "The departure period
for F-1 students is now 30 days, a decrease from the previous 60-day grace period [7]." states both
30 (future-only) and 60 (current), so it is INSERT. A firing sentence with NO such figure states the
future rule ALONE, as current, with nothing anywhere in it naming the rule still in force today --
that is a false statement, not merely a misplaced date, so the guard reports a BLOCK signal instead
(`TemporalQualification.blocked`, `.blocked_source_urls`) for app/pipeline.py to act on, rather than
trying to fix it with an inserted sentence a skimming reader would never reach. The CURRENT-rule
figure set this comparison needs is built by `_current_rule_figures` below, from the same chunks
with the same `_extract_figures` `_future_only_figures` already uses (both delegate to
`_figure_sets`, one shared loop), so the two sets cannot independently drift apart.

`text`/`insertion_count` below are computed exactly as they always were, UNCONDITIONALLY, regardless
of whether a firing sentence is also flagged for BLOCK -- this function reports both signals
truthfully and does not itself choose which one wins. app/pipeline.py is the caller that decides:
when `blocked` is True it discards `text` entirely (a firing sentence in the same answer that ALSO
qualifies for INSERT does not save it -- one false, current-stated future figure is enough to make
the whole answer untrustworthy) and renders a fixed safe message instead, naming the real source(s)
via `blocked_source_urls` (`chunk.resolved_url` if set, else `chunk.source_url` -- the same rule
app/pipeline.py::_citation_url uses, duplicated as `_citation_url` below rather than imported, since
app/pipeline.py itself imports this module and an import the other way would be circular).

THE NO-BRACKET PROPERTY, LOAD-BEARING (see app/pipeline.py's own comment at this module's call site,
which explains why the freshness notice can safely be appended AFTER verify_citations already ran):
the inserted sentence above never contains a citation bracket, so inserting it can never change what
`app/guardrails/citations.py::parse_cited_indices` reads from the answer -- this is asserted by
tests here, not merely assumed.

KNOWN LIMITATION: this is a pattern guard operating on SURFACE numerals, not a semantic one. It does
not know that "30" and "thirty" mean the same figure, does not resolve a figure spelled out in
words, and does not resolve which sentence a pronoun like "it" refers to. Recorded here honestly,
the same way app/guardrails/authority.py records its own known misses, rather than chased with more
patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.db import RetrievedChunk

# Citation bracket: `[7]`, or a comma-separated group like `[1, 3]` -- the same shape
# app/guardrails/citations.py::parse_cited_indices reads. Duplicated here (rather than imported)
# because this module needs the SPAN of the match, to exclude its digits from figure extraction,
# not the parsed indices themselves.
_BRACKET_RE = re.compile(r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]")

# A form number: one uppercase letter, a hyphen, one or more digits, an optional trailing letter --
# I-765, I-20, I-983, I-94, and (deliberately, since the shape is identical) a visa category like
# H-1B. None of these is a rule's figure.
_FORM_NUMBER_RE = re.compile(r"\b[A-Z]-\d+[A-Z]?\b")

# A rendered date phrase, e.g. "September 15, 2026" -- both the day and the year inside it are
# excluded from figure extraction, the same digits app/prompts.py::_rule_date_note and
# app/guardrails/freshness.py::freshness_notice_text render.
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# Measured directly against the live 221-chunk corpus (`SELECT id, rule_effective_date FROM
# documents WHERE rule_effective_date IS NOT NULL`): every dated chunk renders its date in
# ABBREVIATED form -- 'Sept. 15, 2026' (chunks 662, 667-670, 707), 'Nov. 14, 2030' (668, 711),
# 'Sept. 14, 2026' (713) -- and the FULL month name form ("September 15, 2026") appears in ZERO
# dated chunks. Without these abbreviations, `_DATE_PHRASE_RE` matched none of the corpus's own
# dates, so the day and year digits of every real date ("15", "14", "2026", "2030") leaked through
# `_extract_figures` as ordinary figures. Keyed by month number so `_date_stated_pattern` below can
# look up exactly the abbreviations for one date's month without a second, independently-maintained
# list (`_MONTH_ABBREVIATIONS`, used by `_DATE_PHRASE_RE`, is derived from this dict, not hand-kept
# in sync with it).
_MONTH_ABBREVIATIONS_BY_MONTH: dict[int, tuple[str, ...]] = {
    1: ("Jan",),
    2: ("Feb",),
    3: ("Mar",),
    4: ("Apr",),
    5: ("May",),
    6: ("Jun",),
    7: ("Jul",),
    8: ("Aug",),
    9: ("Sep", "Sept"),
    10: ("Oct",),
    11: ("Nov",),
    12: ("Dec",),
}
_MONTH_ABBREVIATIONS = tuple(
    abbreviation
    for abbreviations in _MONTH_ABBREVIATIONS_BY_MONTH.values()
    for abbreviation in abbreviations
)
_DATE_PHRASE_RE = re.compile(
    r"\b(?:"
    + "|".join(_MONTH_NAMES)
    + r")\s+\d{1,2},?\s+\d{4}\b"
    + r"|\b(?:"
    + "|".join(_MONTH_ABBREVIATIONS)
    + r")\.?\s+\d{1,2},?\s+\d{4}\b"
)

# A URL -- e.g. `https://i94.cbp.dhs.gov/home`, preserved verbatim in chunk content wherever the
# source page carries a markdown link (measured on chunks 675/676: "Form I-94 website"'s own link
# text already has its "I-94" excluded by `_FORM_NUMBER_RE`, but the "94" inside the hostname
# `i94.cbp.dhs.gov` is not a form number and was leaking through `_extract_figures` as a bare
# figure). Every digit inside a URL is excluded from figure extraction, not just this one hostname.
_URL_RE = re.compile(r"https?://\S+")

# A numeric date -- e.g. `01/30/2026`, chunk 444's own "Last Reviewed/Updated: 01/30/2026" footer,
# verbatim. THE DEFECT THIS CLOSES, measured directly on the live 221-chunk corpus: chunk 444
# ("STEM OPT Employer Requirements and Responsibilities") is UNDATED (no `rule_effective_date`) and
# is retrieved for the ladder query "How long do I have to leave the US after OPT ends?"; without
# this exclusion, its footer's "30" (from "01/30/2026") landed in `other_figures` in
# `_future_only_figures` below, which disqualified the real future-only "30" (the 30-day departure
# period under the Sept. 15, 2026 rule) from ever firing -- the guard went silent on exactly the
# query it was built to catch. This "Last Reviewed/Updated:" / "Updated:" footer is CORPUS-WIDE,
# not specific to chunk 444: the same shape, with its own MM/DD/YYYY date, also appears on chunks
# 439, 492, 515, 600, and 660 (`SELECT id, content FROM documents` against the live corpus).
#
# Covers every numeric form a US government page uses for this footer: two-digit or one-digit
# month, two-digit or one-digit day, and a four-digit or two-digit year -- MM/DD/YYYY, M/D/YYYY,
# MM/DD/YY, M/D/YY. The year group tries the four-digit form first (`\d{2}(?:\d{2})?` is greedy),
# so "01/30/2026" is excluded as one whole span, not a two-digit-year span plus two stray leftover
# digits.
#
# Deliberately NOT also matching ISO `YYYY-MM-DD`: checked directly against the live corpus
# (`SELECT id, regexp_matches(content, '\d{4}-\d{2}-\d{2}', 'g') FROM documents`), which returns
# zero rows -- no chunk in this corpus uses that form, so a pattern for it would be unmeasured,
# speculative coverage rather than a fix for an observed defect.
_NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2}(?:\d{2})?\b")

# A number, with or without thousands-grouping commas -- "30", "60", "65,000" -- the raw candidate
# before exclusions. Deliberately NOT `\d[\d,]*` (which also swallows a bare trailing comma that is
# just punctuation, e.g. the one right after "I-765," or inside "[7],", stretching the match past
# the very form-number/bracket span that was supposed to exclude it): a comma only extends the match
# when it is followed by exactly three more digits, the actual shape of a thousands separator.
_RAW_FIGURE_RE = re.compile(r"\d+(?:,\d{3})*")

# Sentence split that keeps the separating whitespace as its own list element (a capturing group
# inside re.split does this), so an untouched sentence can be reassembled byte-exact.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])(\s+)")

# A fragment ending in one of this module's own recognized month abbreviations plus its period --
# "Sept.", "Nov." -- measured, direct consequence of recognizing those abbreviations at all
# (`_MONTH_ABBREVIATIONS_BY_MONTH` above): `_SENTENCE_SPLIT_RE` treats ANY ".", followed by
# whitespace, as a sentence end, so "effective Sept. 15, 2026" was torn into "effective Sept." and
# "15, 2026, ..." -- separating the abbreviated month from the very day and year
# `_date_stated_pattern` needs alongside it to recognize the date as already stated. Matched only
# against the END of a fragment (`$`), not used as a lookbehind, so it is not subject to Python
# `re`'s fixed-width lookbehind restriction despite the abbreviations having different lengths.
_MONTH_ABBREVIATION_TRAILING_RE = re.compile(r"\b(?:" + "|".join(_MONTH_ABBREVIATIONS) + r")\.$")


def _format_date(value: date) -> str:
    # Deliberately duplicated from app/guardrails/freshness.py's own `_format_date` (and
    # app/prompts.py's own copy) -- same "%-d is not portable across platforms" reasoning, and the
    # same avoid-a-cross-module-private-import choice both of those already make.
    return f"{value:%B} {value.day}, {value.year}"


def _date_stated_pattern(value: date) -> re.Pattern[str]:
    """A regex matching every plausible rendering of `value` this corpus or a model copying its
    wording might use: the full month name or either of its abbreviations (each with an optional
    trailing period, the same tolerance `_DATE_PHRASE_RE` gives the corpus's own dates), the day
    with or without a leading zero, an optional comma before the year, and the exact year -- built
    from `value` alone, never hardcoded to any one date, so the same function works for the rule's
    effective date, `today`, or any other date this module is ever asked about.

    This replaces a plain `_format_date(value) not in sentence` containment test (ALGORITHM step 4):
    that test only recognizes the one literal string `_format_date` produces ("September 15,
    2026"), but the corpus itself never renders a date that way -- every dated chunk uses the
    abbreviated form ("Sept. 15, 2026", see `_MONTH_ABBREVIATIONS_BY_MONTH`'s own comment) -- so a
    model answer that copies the corpus's own wording would not have been recognized as already
    stating the date, and this guard would have inserted a redundant qualifier right next to a
    sentence that already carries it.

    Deliberately anchored to `value.day` and `value.year` as literal digits (not `\\d+`), so a
    different day or year -- a different date entirely -- does not match.
    """
    month_names = (
        _MONTH_NAMES[value.month - 1],
        *_MONTH_ABBREVIATIONS_BY_MONTH[value.month],
    )
    month_alternation = "|".join(re.escape(name) for name in month_names)
    return re.compile(rf"\b(?:{month_alternation})\.?\s+0?{value.day}\b,?\s+{value.year}\b")


def _is_year(digits_only: str) -> bool:
    return len(digits_only) == 4 and digits_only.isdigit() and 1900 <= int(digits_only) <= 2100


def _excluded_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for regex in (_BRACKET_RE, _FORM_NUMBER_RE, _DATE_PHRASE_RE, _URL_RE, _NUMERIC_DATE_RE):
        spans.extend((m.start(), m.end()) for m in regex.finditer(text))
    return spans


def _extract_figures(text: str) -> set[str]:
    """Every numeric figure in `text`, normalized (commas stripped), after excluding a citation
    bracket, a form number, a bare four-digit year, a rendered date's own digits, a digit inside a
    URL, and a digit inside a numeric date (`01/30/2026`) -- see this module's docstring, ALGORITHM
    step 2. Used both on a retrieved chunk's own content (to build the future-only figure set) and
    on a sentence of the generated answer (to test whether it states one), so the two sides of the
    comparison are extracted the exact same way.
    """
    spans = _excluded_spans(text)
    figures: set[str] = set()
    for match in _RAW_FIGURE_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in spans):
            continue
        normalized = match.group(0).replace(",", "")
        if _is_year(normalized):
            continue
        figures.add(normalized)
    return figures


def _split_sentences_with_separators(text: str) -> list[str]:
    """`_SENTENCE_SPLIT_RE`'s own split, then re-merges any split that landed right after one of
    this module's recognized month abbreviations (`_MONTH_ABBREVIATION_TRAILING_RE`) AND right
    before what looks like a day number -- e.g. "effective Sept." / " " / "15, 2026, ..." is
    re-joined into one fragment, "effective Sept. 15, 2026, ...", so the date's month is never torn
    away from its own day and year. Scoped to exactly that shape (abbreviation-period immediately
    followed by a digit): a genuine sentence that happens to end in a month abbreviation with
    nothing date-like after it (e.g. "...last Sept.") is left split exactly as
    `_SENTENCE_SPLIT_RE` alone would split it, since nothing after it starts with a digit.
    Byte-exact either way: every raw fragment `_SENTENCE_SPLIT_RE.split` produced is still present
    in the result, only concatenated together rather than dropped or altered.
    """
    raw_parts = _SENTENCE_SPLIT_RE.split(text)
    result: list[str] = []
    buffer = raw_parts[0] if raw_parts else ""
    i = 1
    while i < len(raw_parts):
        separator = raw_parts[i]
        next_sentence = raw_parts[i + 1] if i + 1 < len(raw_parts) else ""
        if _MONTH_ABBREVIATION_TRAILING_RE.search(buffer) and next_sentence[:1].isdigit():
            buffer += separator + next_sentence
        else:
            result.append(buffer)
            result.append(separator)
            buffer = next_sentence
        i += 2
    result.append(buffer)
    return result


@dataclass(frozen=True)
class _FigureSets:
    """The two figure sets `qualify_future_dated_figures` needs, both derived from the SAME single
    pass over `chunks` (see `_figure_sets` below) so they cannot independently drift apart --
    `future_only` is exactly what `_future_only_figures` used to compute standalone, and `current`
    is the CURRENT-rule figure set the BLOCK-vs-INSERT split (module docstring) tests a firing
    sentence's own figures against.
    """

    future_only: dict[str, set[date]]
    current: set[str]


def _figure_sets(chunks: list[RetrievedChunk], *, today: date) -> _FigureSets:
    """One pass over `chunks`, extracting each one's figures with `_extract_figures` exactly once,
    and sorting them into `future_only` (ALGORITHM step 1: a figure found in a future-dated chunk
    and in NO chunk that is undated or dated on/before `today`) and `current` (every figure found in
    a chunk that is undated or dated on/before `today` -- regardless of whether that same figure is
    ALSO future-only for some other chunk). `_future_only_figures` and `_current_rule_figures` below
    are both thin wrappers over this one function, rather than two independent loops, specifically
    so the two sets can never disagree about which chunks count as "future-dated" or how a chunk's
    figures are extracted.
    """
    future_dates_by_figure: dict[str, set[date]] = {}
    current_figures: set[str] = set()
    for chunk in chunks:
        figures = _extract_figures(chunk.content)
        if chunk.rule_effective_date is not None and chunk.rule_effective_date > today:
            for figure in figures:
                future_dates_by_figure.setdefault(figure, set()).add(chunk.rule_effective_date)
        else:
            current_figures |= figures
    future_only = {
        figure: dates
        for figure, dates in future_dates_by_figure.items()
        if figure not in current_figures
    }
    return _FigureSets(future_only=future_only, current=current_figures)


def _future_only_figures(chunks: list[RetrievedChunk], *, today: date) -> dict[str, set[date]]:
    """Every FUTURE-ONLY figure (ALGORITHM step 1), mapped to the set of future
    `rule_effective_date` values of the chunk(s) it was found in -- a figure could in principle come
    from more than one future-dated chunk carrying different dates, so this is a set, not a single
    date. A thin wrapper over `_figure_sets` (see that function's own docstring for why this and
    `_current_rule_figures` share one loop rather than each running their own).
    """
    return _figure_sets(chunks, today=today).future_only


def _current_rule_figures(chunks: list[RetrievedChunk], *, today: date) -> set[str]:
    """Every figure appearing in a chunk that is NOT future-dated -- `rule_effective_date` is
    `None` or on/before `today` -- the CURRENT-rule figure set the BLOCK-vs-INSERT split (module
    docstring, "BLOCK VS INSERT") tests a firing sentence's own figures against. A thin wrapper over
    `_figure_sets`, sharing its one loop and its one `_extract_figures` call per chunk with
    `_future_only_figures` above, so the two sets cannot drift apart via two independently
    maintained implementations.
    """
    return _figure_sets(chunks, today=today).current


def _future_chunks_for_figure(
    chunks: list[RetrievedChunk], figure: str, *, today: date
) -> list[RetrievedChunk]:
    """Every chunk in `chunks` that is future-dated (`rule_effective_date` strictly after `today`)
    and whose own content contains `figure` (via the same `_extract_figures` every other figure
    extraction in this module uses) -- the chunk(s) a BLOCK signal blames for a given firing
    sentence's future-only figure, so `qualify_future_dated_figures` can name their `source_url` in
    `TemporalQualification.blocked_source_urls`.
    """
    return [
        chunk
        for chunk in chunks
        if chunk.rule_effective_date is not None
        and chunk.rule_effective_date > today
        and figure in _extract_figures(chunk.content)
    ]


def _citation_url(chunk: RetrievedChunk) -> str:
    """Duplicated from app/pipeline.py's own `_citation_url` (not imported -- app/pipeline.py
    imports THIS module, so importing the other way would be circular): `resolved_url` if set, else
    `source_url`, so a BLOCK message names the exact URL a citation to the same chunk would use.
    """
    return chunk.resolved_url or chunk.source_url


@dataclass(frozen=True)
class TemporalQualification:
    """`text` is `answer_text` with zero or more derived sentences inserted (see this module's
    docstring); `insertion_count` is exactly how many were inserted, for telemetry and tests --
    `insertion_count == 0` implies `text == answer_text`, unchanged. Both are computed
    UNCONDITIONALLY, exactly as before the BLOCK/INSERT split existed, regardless of `blocked`.

    `blocked` (module docstring, "BLOCK VS INSERT") is True if at least one firing sentence states a
    future-dated rule's figure with no accompanying CURRENT-rule figure anywhere in that same
    sentence -- asserting the future rule alone, as though it were already in force, rather than
    merely misplacing the date. `blocked_source_urls` is the `source_url` (`chunk.resolved_url` if
    set, else `chunk.source_url`) of every future-dated chunk whose figure triggered a BLOCK,
    first-encountered order, deduplicated; empty whenever `blocked` is False. This function only
    reports both signals truthfully -- app/pipeline.py is the caller that decides what to do with
    `blocked` (discard `text` and render a fixed safe message instead; see that module's call site).
    """

    text: str
    insertion_count: int
    blocked: bool = False
    blocked_source_urls: tuple[str, ...] = ()


def qualify_future_dated_figures(
    answer_text: str, chunks: list[RetrievedChunk], *, today: date
) -> TemporalQualification:
    """See this module's docstring for the defect this closes and the exact algorithm, including
    "BLOCK VS INSERT" for how a firing sentence is additionally classified. Pure: no network, no DB,
    no model -- `chunks` is the already-retrieved list app/pipeline.py has in hand by the time this
    runs.
    """
    figure_sets = _figure_sets(chunks, today=today)
    future_only = figure_sets.future_only
    current_figures = figure_sets.current
    if not future_only:
        return TemporalQualification(text=answer_text, insertion_count=0)

    parts = _split_sentences_with_separators(answer_text)
    sentences = parts[0::2]
    separators = parts[1::2]

    result: list[str] = []
    insertion_count = 0
    blocked = False
    blocked_urls: list[str] = []

    for i, sentence in enumerate(sentences):
        sentence_figures = _extract_figures(sentence)
        relevant_dates: set[date] = set()
        for figure in sentence_figures:
            relevant_dates |= future_only.get(figure, set())

        unstated_dates = sorted(
            d for d in relevant_dates if not _date_stated_pattern(d).search(sentence)
        )

        if unstated_dates:
            rule_date = unstated_dates[0]
            derived = (
                f"That figure comes from a rule that takes effect on {_format_date(rule_date)}. "
                f"It is not the rule in force today, {_format_date(today)}."
            )
            result.append(f"{sentence} {derived}")
            insertion_count += 1

            # BLOCK VS INSERT (module docstring): this firing sentence also asserts the future
            # rule alone, as current, if none of ITS OWN figures is a current-rule figure -- a
            # different sentence elsewhere in the same answer stating "60" does not save this one,
            # by design (a reader skimming this sentence never reaches that other one either).
            if not (sentence_figures & current_figures):
                blocked = True
                unstated_date_set = set(unstated_dates)
                firing_figures = {
                    figure
                    for figure in sentence_figures
                    if future_only.get(figure, set()) & unstated_date_set
                }
                for figure in firing_figures:
                    for chunk in _future_chunks_for_figure(chunks, figure, today=today):
                        url = _citation_url(chunk)
                        if url not in blocked_urls:
                            blocked_urls.append(url)
        else:
            result.append(sentence)

        if i < len(separators):
            result.append(separators[i])

    return TemporalQualification(
        text="".join(result),
        insertion_count=insertion_count,
        blocked=blocked,
        blocked_source_urls=tuple(blocked_urls),
    )
