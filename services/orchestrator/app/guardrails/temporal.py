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

1. A numeric FIGURE is NOT-IN-FORCE-ONLY if it appears in at least one retrieved chunk whose
   `rule_status` is not in force (`app/rule_status.py::is_in_force` is False -- `scheduled`,
   `enjoined`, or `not_in_force`), and appears in NO retrieved chunk that is in force or unannotated
   (`is_in_force` is True: an explicit `in_force` status, or no `rule_status` annotation at all --
   see `app/rule_status.py::is_in_force`'s own docstring for why the two are folded together). A
   figure appearing in both a not-in-force chunk and an in-force/unannotated chunk is not
   not-in-force-only, and must never fire -- that is exactly the shape of figure this module has no
   basis for qualifying (it is either unchanged by the contested or dated rule, or the model could
   equally have drawn it from a current source). RENAMED 2026-09-19 from FUTURE-ONLY: the test is no
   longer "is this chunk dated after today", it is "is this chunk's rule NOT in force" -- see this
   module's docstring, "CURATOR-STATED FORCE".
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
4. A sentence fires when it contains at least one not-in-force-only figure AND, for a chunk that
   carries a `rule_effective_date` (only `scheduled` requires one; `enjoined`/`not_in_force` may
   have none), does not already STATE that date, checked by `_date_stated_pattern` below against
   every plausible rendering of it -- full or abbreviated month (each with an optional trailing
   period), the day with or without a leading zero, and an optional comma before the year -- not
   only the one literal string `_format_date` produces. This has to be broader than a literal
   `_format_date` containment test: the corpus itself renders every dated chunk's own date in
   abbreviated form ("Sept. 15, 2026"), so a model answer that copies the corpus's own wording would
   never satisfy a check for the literal "September 15, 2026" -- and the guard would insert a
   redundant qualifier next to a sentence that already carries the date. A chunk with no
   `rule_effective_date` at all (`enjoined`/`not_in_force`) has no date to already-state, so it is
   always treated as unstated when its figure fires. When a sentence's not-in-force-only figures
   trace back to more than one provenance (one dated, one not, or two different statuses), the one
   sorted first by `_provenance_sort_key` below (a dated provenance, earliest date first, ahead of
   any undated one) is used -- one inserted sentence per firing sentence, never one per figure or
   one per provenance.
5. Exactly ONE derived sentence is inserted immediately after a firing sentence. Its wording
   depends on the chosen provenance's `rule_status` -- never the figure, the topic, a URL, or any
   description of what the rule itself says, the same discipline `app/guardrails/freshness.py::
   freshness_notice_text` and `app/prompts.py::_rule_date_note` already follow. `today` is the only
   other input, for the "not the rule in force today, <date>" clause every wording shares:

       scheduled:     That figure comes from a rule that takes effect on September 15, 2026. It is
                       not the rule in force today, September 11, 2026.
       enjoined:      That figure comes from a rule that is blocked by a court order and is not in
                       force today, September 11, 2026.
       not_in_force:  That figure comes from a rule that is not in force today, September 11, 2026.

   `in_force` and unannotated chunks never reach this step at all: their figures join the CURRENT
   set in step 1 and can never be not-in-force-only.

BLOCK VS INSERT (2026-09-12): a firing sentence does not always just get that inserted sentence.
Ten production runs of "What is the grace period after OPT ends?" on 2026-09-12, read by their
opening sentence: 3/10 stated the current rule correctly, 5/10 stated the FUTURE rule as though it
were already current ("The current grace period after post-completion OPT ... ends is 30 days",
three days before that becomes true), 2/10 neither. This guard fired on exactly those five and none
of the other five -- the detection signal above is reliable -- but appending a correction two
sentences later does not unsay a false figure asserted as current in the OPENING sentence, for a
reader skimming it. An honest non-answer naming the source is better than that.

The split: a firing sentence whose OWN figures (`_extract_figures` on the SENTENCE, the same
extractor `_not_in_force_only_figures` above uses on chunk content) include at least one figure that
ALSO appears in a CURRENT chunk (`is_in_force(chunk.rule_status)` is True) still gets the single
derived sentence inserted, exactly as before this split existed: both the current and the not-
in-force rule are present in the sentence, only the qualification is missing -- "The departure
period for F-1 students is now 30 days, a decrease from the previous 60-day grace period [7]."
states both 30 (not-in-force-only) and 60 (current), so it is INSERT. A firing sentence with NO such
figure states the not-in-force rule ALONE, as current, with nothing anywhere in it naming the rule
still in force today -- that is a false statement, not merely a missing qualifier, so the guard
reports a BLOCK signal instead (`TemporalQualification.blocked`, `.blocked_source_urls`) for
app/pipeline.py to act on, rather than trying to fix it with an inserted sentence a skimming reader
would never reach. THIS AXIS IS UNCHANGED BY THE 2026-09-19 FIX and does not itself vary by status:
an `enjoined` figure asserted alone, as current, is exactly as false as a `scheduled` one asserted
alone, as current, so `enjoined` is not made "always block" -- the sentence still has to lack an
accompanying current-rule figure to block, the same test every status shares. The CURRENT-rule
figure set this comparison needs is built by `_current_rule_figures` below, from the same chunks
with the same `_extract_figures` `_not_in_force_only_figures` already uses (both delegate to
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

CURATOR-STATED FORCE (2026-09-19, docs/adr/0023-curator-rule-status.md; see REPORT.md, "The
injunction, and why this is not fixed"). Every comparison in this module used to be
`chunk.rule_effective_date > today`: a FUTURE-ONLY figure was one found only in a chunk dated after
today. On 2026-09-15, the DHS fixed-period-of-admission rule's own published effective date, that
comparison silently flipped from true to false and the guard went inert -- except a federal court
had enjoined the rule the day before, so the 30-day figure was exactly as false on the 15th as it
was on the 14th, and the guard fell silent on the one day it mattered most. This module no longer
compares any date to `today` to decide whether a chunk's rule counts. It asks
`app/rule_status.py::is_in_force(chunk.rule_status)` -- a curator-stated fact, `status ==
"in_force"`, never arithmetic -- and the renamed concept below, NOT_IN_FORCE-ONLY (formerly
FUTURE-ONLY), is built from that predicate instead of a date. `rule_effective_date` still appears
in this module's derived sentences (a `scheduled` rule's message still names its date), but it is
now output, never input to the force decision.

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
from app.rule_status import RuleStatus, is_in_force

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
# `_not_in_force_only_figures` below (named `_future_only_figures` at the time this was measured --
# see this module's docstring, "CURATOR-STATED FORCE"), which disqualified the real not-in-force
# "30" (the 30-day departure period under the Sept. 15, 2026 rule) from ever firing -- the guard
# went silent on exactly the query it was built to catch. This "Last Reviewed/Updated:" /
# "Updated:" footer is CORPUS-WIDE,
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
    step 2. Used both on a retrieved chunk's own content (to build the not-in-force-only figure
    set) and on a sentence of the generated answer (to test whether it states one), so the two
    sides of the comparison are extracted the exact same way.
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
class _NotInForceProvenance:
    """One not-in-force chunk's status and (optional) `rule_effective_date`, tracked per
    not-in-force-only figure so `qualify_future_dated_figures` can render the right wording (a
    `scheduled` figure's message names its date; an `enjoined`/`not_in_force` figure's message
    never does, since neither carries one in `app/rule_status.py`'s own per-state contract) and can
    tell whether a firing sentence already states it. Frozen and hashable so it can sit inside the
    `set[_NotInForceProvenance]` `_FigureSets.not_in_force_only` maps each figure to -- a figure can
    in principle come from more than one not-in-force chunk, carrying different statuses or dates.
    """

    status: str
    rule_effective_date: date | None


def _provenance_sort_key(provenance: _NotInForceProvenance) -> tuple:
    """Which of several unstated provenances for one firing sentence gets to name the derived
    sentence's wording (ALGORITHM step 4): a provenance carrying a date sorts before one that
    doesn't, and among dated provenances the earliest date wins -- exactly the "earliest unstated
    date" tie-break this module used before the 2026-09-19 fix, now extended to also order the
    undated (`enjoined`/`not_in_force` with no `rule_effective_date`) case deterministically, by
    status name, since nothing else distinguishes two such provenances.
    """
    if provenance.rule_effective_date is not None:
        return (0, provenance.rule_effective_date)
    return (1, provenance.status)


@dataclass(frozen=True)
class _FigureSets:
    """The two figure sets `qualify_future_dated_figures` needs, both derived from the SAME single
    pass over `chunks` (see `_figure_sets` below) so they cannot independently drift apart --
    `not_in_force_only` is exactly what `_not_in_force_only_figures` used to compute standalone, and
    `current` is the CURRENT-rule figure set the BLOCK-vs-INSERT split (module docstring) tests a
    firing sentence's own figures against.
    """

    not_in_force_only: dict[str, set[_NotInForceProvenance]]
    current: set[str]


def _figure_sets(chunks: list[RetrievedChunk]) -> _FigureSets:
    """One pass over `chunks`, extracting each one's figures with `_extract_figures` exactly once,
    and sorting them into `not_in_force_only` (ALGORITHM step 1: a figure found in a chunk whose
    `rule_status` is not in force, per `app/rule_status.py::is_in_force`, and in NO chunk that is in
    force or unannotated) and `current` (every figure found in a chunk that is in force or
    unannotated -- regardless of whether that same figure is ALSO not-in-force-only for some other
    chunk). `_not_in_force_only_figures` and `_current_rule_figures` below are both thin wrappers
    over this one function, rather than two independent loops, specifically so the two sets can
    never disagree about which chunks count as not-in-force or how a chunk's figures are extracted.

    NO DATE COMPARISON HERE (2026-09-19 fix; see module docstring, "CURATOR-STATED FORCE"): the
    branch below reads only `chunk.rule_status` through `is_in_force`, never `chunk.
    rule_effective_date` against a clock. `today` is no longer a parameter of this function at all
    -- it has nothing left to do here, and removing it is what makes it impossible for this
    function to regress back into a date comparison by accident.
    """
    not_in_force_by_figure: dict[str, set[_NotInForceProvenance]] = {}
    current_figures: set[str] = set()
    for chunk in chunks:
        figures = _extract_figures(chunk.content)
        if is_in_force(chunk.rule_status):
            current_figures |= figures
        else:
            provenance = _NotInForceProvenance(
                status=chunk.rule_status, rule_effective_date=chunk.rule_effective_date
            )
            for figure in figures:
                not_in_force_by_figure.setdefault(figure, set()).add(provenance)
    not_in_force_only = {
        figure: provenances
        for figure, provenances in not_in_force_by_figure.items()
        if figure not in current_figures
    }
    return _FigureSets(not_in_force_only=not_in_force_only, current=current_figures)


def _not_in_force_only_figures(
    chunks: list[RetrievedChunk],
) -> dict[str, set[_NotInForceProvenance]]:
    """Every NOT-IN-FORCE-ONLY figure (ALGORITHM step 1; renamed 2026-09-19 from
    `_future_only_figures`), mapped to the set of `_NotInForceProvenance` (status + optional date)
    of the chunk(s) it was found in -- a figure could in principle come from more than one
    not-in-force chunk carrying different statuses or dates, so this is a set, not a single value.
    A thin wrapper over `_figure_sets` (see that function's own docstring for why this and
    `_current_rule_figures` share one loop rather than each running their own).
    """
    return _figure_sets(chunks).not_in_force_only


def _current_rule_figures(chunks: list[RetrievedChunk]) -> set[str]:
    """Every figure appearing in a chunk that IS in force -- `is_in_force(chunk.rule_status)` is
    True, an explicit `in_force` status or no annotation at all -- the CURRENT-rule figure set the
    BLOCK-vs-INSERT split (module docstring, "BLOCK VS INSERT") tests a firing sentence's own
    figures against. A thin wrapper over `_figure_sets`, sharing its one loop and its one
    `_extract_figures` call per chunk with `_not_in_force_only_figures` above, so the two sets
    cannot drift apart via two independently maintained implementations.
    """
    return _figure_sets(chunks).current


def _not_in_force_chunks_for_figure(
    chunks: list[RetrievedChunk], figure: str
) -> list[RetrievedChunk]:
    """Every chunk in `chunks` whose `rule_status` is not in force (`app/rule_status.py::
    is_in_force` is False) and whose own content contains `figure` (via the same `_extract_figures`
    every other figure extraction in this module uses) -- the chunk(s) a BLOCK signal blames for a
    given firing sentence's not-in-force-only figure, so `qualify_future_dated_figures` can name
    their `source_url` in `TemporalQualification.blocked_source_urls`.
    """
    return [
        chunk
        for chunk in chunks
        if not is_in_force(chunk.rule_status) and figure in _extract_figures(chunk.content)
    ]


def _citation_url(chunk: RetrievedChunk) -> str:
    """Duplicated from app/pipeline.py's own `_citation_url` (not imported -- app/pipeline.py
    imports THIS module, so importing the other way would be circular): `resolved_url` if set, else
    `source_url`, so a BLOCK message names the exact URL a citation to the same chunk would use.
    """
    return chunk.resolved_url or chunk.source_url


# The three derived-sentence wordings this module can insert, one per not-in-force `RuleStatus`
# (`in_force` and unannotated chunks never reach this point at all -- see `_figure_sets`). Named
# module-level constants, not inline literals in the branch below, for the same reason
# app/prompts.py's own note templates are (see that module's docstring, "THE PROMPT RULE 4 TRAP"):
# a reader -- and a future status added to `app/rule_status.py::RuleStatus` -- has exactly one place
# to look for what this module actually says about each state.
#
# `enjoined` deliberately never says "takes effect on": that phrase asserts the rule has a date it
# will become law on its own, which is false for a rule a court has blocked -- it may never take
# effect at all, or take effect on a different date than the one DHS originally published. Same
# reasoning for `not_in_force`: a vacated or withdrawn rule is not "taking effect" on any date.
_SCHEDULED_TEMPLATE = (
    "That figure comes from a rule that takes effect on {date}. It is not the rule in force "
    "today, {today}."
)
_ENJOINED_TEMPLATE = (
    "That figure comes from a rule that is blocked by a court order and is not in force today, "
    "{today}."
)
_NOT_IN_FORCE_TEMPLATE = "That figure comes from a rule that is not in force today, {today}."


def _derived_sentence(provenance: _NotInForceProvenance, *, today: date) -> str:
    """The ONE sentence inserted after a firing sentence (ALGORITHM step 5), chosen by
    `provenance.status`. `in_force` never reaches here (module docstring: only a not-in-force
    provenance is ever passed in) -- an unrecognized status raises rather than silently falling
    back to one of the three wordings below, which would put a wrong claim into a rendered answer
    for a status this module has not actually been taught about (see `app/rule_status.py`'s own
    comment on the parametrized test that is supposed to catch exactly this gap before it ships).
    """
    if provenance.status == RuleStatus.SCHEDULED.value:
        return _SCHEDULED_TEMPLATE.format(
            date=_format_date(provenance.rule_effective_date), today=_format_date(today)
        )
    if provenance.status == RuleStatus.ENJOINED.value:
        return _ENJOINED_TEMPLATE.format(today=_format_date(today))
    if provenance.status == RuleStatus.NOT_IN_FORCE.value:
        return _NOT_IN_FORCE_TEMPLATE.format(today=_format_date(today))
    raise ValueError(
        f"temporal.py has no derived-sentence wording for rule_status={provenance.status!r} -- "
        "add one (and a parametrized test for it) before this status can reach the guard."
    )


@dataclass(frozen=True)
class TemporalQualification:
    """`text` is `answer_text` with zero or more derived sentences inserted (see this module's
    docstring); `insertion_count` is exactly how many were inserted, for telemetry and tests --
    `insertion_count == 0` implies `text == answer_text`, unchanged. Both are computed
    UNCONDITIONALLY, exactly as before the BLOCK/INSERT split existed, regardless of `blocked`.

    `blocked` (module docstring, "BLOCK VS INSERT") is True if at least one firing sentence states a
    not-in-force rule's figure with no accompanying CURRENT-rule figure anywhere in that same
    sentence -- asserting the not-in-force rule alone, as though it were already in force, rather
    than merely missing its qualifier. `blocked_source_urls` is the `source_url` (`chunk.
    resolved_url` if set, else `chunk.source_url`) of every not-in-force chunk whose figure
    triggered a BLOCK, first-encountered order, deduplicated; empty whenever `blocked` is False.
    This function only reports both signals truthfully -- app/pipeline.py is the caller that decides
    what to do with `blocked` (discard `text` and render a fixed safe message instead; see that
    module's call site).
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
    runs. `today` is used only for the "not the rule in force today, <today>" clause every wording
    shares, and to test whether a sentence already states a `scheduled` chunk's own date -- never to
    decide which chunks count as not-in-force (that is `_figure_sets`'s job, and it takes no `today`
    at all -- see its own docstring).

    Kept as `qualify_future_dated_figures`, not renamed to something like
    `qualify_not_in_force_figures`, deliberately: app/pipeline.py's own call site, its docstring,
    and every test that exercises this function through the pipeline already name this function:
    renaming it would touch every one of those for a label change with no behavioral content, and
    the INTERNAL concept the module docstring says to rename -- future_only -> not_in_force_only --
    is the one that mattered, because that name is what used to imply a date comparison.
    """
    figure_sets = _figure_sets(chunks)
    not_in_force_only = figure_sets.not_in_force_only
    current_figures = figure_sets.current
    if not not_in_force_only:
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
        relevant_provenances: set[_NotInForceProvenance] = set()
        for figure in sentence_figures:
            relevant_provenances |= not_in_force_only.get(figure, set())

        # A provenance with no date (enjoined/not_in_force carrying none) has no literal date
        # phrase to already-state, so it is always treated as unstated -- ALGORITHM step 4.
        unstated = sorted(
            (
                p
                for p in relevant_provenances
                if p.rule_effective_date is None
                or not _date_stated_pattern(p.rule_effective_date).search(sentence)
            ),
            key=_provenance_sort_key,
        )

        if unstated:
            chosen = unstated[0]
            derived = _derived_sentence(chosen, today=today)
            result.append(f"{sentence} {derived}")
            insertion_count += 1

            # BLOCK VS INSERT (module docstring): this firing sentence also asserts the
            # not-in-force rule alone, as current, if none of ITS OWN figures is a current-rule
            # figure -- a different sentence elsewhere in the same answer stating "60" does not
            # save this one, by design (a reader skimming this sentence never reaches that other
            # one either).
            if not (sentence_figures & current_figures):
                blocked = True
                unstated_set = set(unstated)
                firing_figures = {
                    figure
                    for figure in sentence_figures
                    if not_in_force_only.get(figure, set()) & unstated_set
                }
                for figure in firing_figures:
                    for chunk in _not_in_force_chunks_for_figure(chunks, figure):
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
