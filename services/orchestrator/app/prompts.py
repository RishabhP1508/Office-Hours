"""Prompt templates.

Two system prompts: `SYSTEM_PROMPT` for a normal factual question, `REFUSAL_SYSTEM_PROMPT` for a
question app/guardrails/classifier.py has classified as advice-seeking. Both still answer from the
retrieved context with bracket citations -- an advice-seeking question still retrieves and still
states the general rule (see app/pipeline.py and docs/adr/0002-advice-vs-information-line.md for
why a canned refusal template, with no retrieval, is not what happens instead).
REFUSAL_SYSTEM_PROMPT additionally forbids choosing an option for the reader, predicting their
outcome, or estimating their odds, and directs them to their DSO or a licensed immigration
attorney.

**Citation convention: a bracket index into the numbered context passages, never a URL.** This
module used to tell the model to cite "using the source's URL, exactly as given in the context",
while eval/metrics.py has always counted bracket indices (`[1]`, `[2]`, ...) -- a spec mismatch,
not the model disobeying, and it is why 9 of 21 Phase 3 answers inlined a raw URL instead of a
bracket number. Rule 2 below is the fix: cite the passage's bracket number, never paste a URL into
the answer text, and never append a trailing "Source URLs:" list -- the interface renders
citations separately from the answer. `strip_source_list_block` below is the belt-and-braces
backstop for a model that appends one anyway.

**Citation markup must be a plain ASCII bracket, never the model's own native citation format**
(Phase 8 round 4). gpt-oss:120b (the Phase 8 production generator) frequently cites in its own
training-format markup instead of the bracket convention above, e.g. "...work permit【1†L2-L5】."
instead of "...work permit [1].". `app/guardrails/citations.py::parse_cited_indices` only ever
recognizes "[N]", so an answer using ONLY that native markup reads as carrying zero citations at
all and was wrongly BLOCKED_UNVERIFIED despite the model genuinely grounding its answer in the
numbered context passage it named. Measured over 15 real hosted generations on the 5 questions this
caused to be blocked: 9 used "[N]", 6 used the "【N†...】" form, and ZERO produced no citation at
all -- the model always cites something, the project just could not always read it. Rule 2 below
now says explicitly not to use that form; `normalize_native_citation_markup` further down is the
belt-and-braces backstop that rewrites it to "[N]" BEFORE verification runs, for a model that uses
it anyway (see that function's own docstring for exactly what it does and does not do -- in
particular, it never invents a citation that was not there, and an out-of-range N still gets
rejected exactly as an out-of-range "[N]" already was).

**This module still carries three rules a guardrail also enforces, on purpose, not by oversight:**
rule 5 (never give advice) and rule 3 (say plainly when the sources do not cover it) were NOT
retired when the Phase 4 guardrails landed -- this docstring used to say the prompt "grows in
Phase 4 when the advice-vs-information classifier and citation verifier take over some of this
work"; what actually happened is that the guardrails were added ALONGSIDE these rules, and neither
rule was removed. Rule 5 stays because the classifier's Layer 1/Layer 2
(app/guardrails/classifier.py) can still miss an advice-seeking phrasing nobody anticipated; when
that happens, this rule is the only thing left standing between the reader and an answer that
tells them what to do. Rule 3 stays because the programmatic no-answer check (app/pipeline.py,
Settings.NO_ANSWER_MAX_DISTANCE) only catches the case where nothing retrieved is close enough to
the question at all -- a retrieved chunk can be topically close and still not actually contain the
answer, and only the model reading it can notice that gap. Both are real belt-and-braces defenses,
not decorative leftovers; see docs/adr/0002-advice-vs-information-line.md.

Rule 7 (SYSTEM_PROMPT) / rule 5 (REFUSAL_SYSTEM_PROMPT), added after a live incident where a
prompt-injected "SYSTEM:" line inside the user's own question got the model to open its answer with
"This answer reflects official USCIS guidance.", follows the same belt-and-braces shape: telling the
model not to claim official authority does not stop a model that is asked to anyway, so
app/guardrails/authority.py::verify_no_authority_claim checks the generated text itself,
programmatically, for exactly that claim (see app/pipeline.py step 7). The prompt rule stays
because it is still the cheaper fix for the common case; the guardrail stays because the prompt
rule is not enforcement.

**Plain-language shaping** (rule 6 below): lead with the direct answer before any detail, define
jargon and form numbers inline the first time they appear, prefer short sentences, and avoid
headings or heavy bold formatting. Phase 3's measured `reading_grade_level` was 17.579 for an
audience of stressed, often non-native-English-speaking readers on their phones; improving the
prompt is the sanctioned way to bring that down under CLAUDE.md's eval carve-out -- the metric and
the rubric are never touched to move the number.

**Red-team fix (2026-09-07): `rule_effective_date` now reaches the model, not just the prose.**
Before this fix, `format_context` rendered every passage identically -- `[N] Source: <url>` plus
content -- whether or not that passage carried a dated rule change (`RetrievedChunk.
rule_effective_date`, e.g. the DHS fixed-period-of-admission final rule effective 2026-09-15). Rule
4 below (SYSTEM_PROMPT) / rule 1 (REFUSAL_SYSTEM_PROMPT) told the model to state both the current
and a dated replacement rule when the context held both, but the model had no way to tell WHICH
passage was the dated one except by reading and understanding the prose -- and measured against six
real phrasings of "how long do I have to leave the US after my program ends," run six times each
against the live production stack, that worked on roughly 1 run in 6. `format_context` now appends
a short, mechanically-generated note ("this passage describes a rule that takes effect on <date>" /
"took effect on <date>") to any passage whose chunk carries a `rule_effective_date`, computed from a
`today` passed in by the caller (app/pipeline.py) -- never read from the clock inside this module,
the same discipline app/guardrails/freshness.py::build_freshness already follows for its own
`today` parameter. A passage with no `rule_effective_date` renders exactly as it did before this
fix, byte-for-byte. Rules 4/1 were sharpened to bind explicitly to this note rather than to the
vaguer "current rule and a dated replacement" language alone, so the instruction and the thing it
is instructing about now use the same vocabulary.
"""

import hashlib
import re
from datetime import UTC, date, datetime

SYSTEM_PROMPT = """You are Office Hours, an assistant that answers factual questions about F-1, \
OPT, STEM OPT, and H-1B immigration rules for international students and workers.

Rules you must follow:
1. Answer ONLY using the context passages given to you below. Do not use outside knowledge, \
and do not guess.
2. For every factual claim, cite the passage's bracket number, exactly as given in the context, \
for example [2]. Use a plain ASCII bracket like [2] -- never a full-width bracket citation like \
【1†source】, and never any other citation format. Cite only a number that appears in the context \
below, and never invent one. Never paste a URL into your answer text, and never add a \
"Source URLs:" list, a "Sources:" section, or any similar trailing list of links at the end of \
your answer -- the interface renders citations separately from your answer text.
3. If the context does not answer the question, say plainly that your sources do not cover it. Do \
not stretch an unrelated or partial passage into a confident answer.
4. Some passages below carry a note that the rule they describe "took effect on <date>" (on or \
before today) or "takes effect on <date>" (a future date, not yet in effect). When the passages \
you were given mix a "takes effect on" future-dated passage with a passage on the same topic that \
carries no such note, or a "took effect on" note, you are looking at a rule that is changing: \
state the current rule together with the date it applies until, and the replacement rule together \
with the date it takes effect, using each passage's own dates. Never state a single fact as though \
only one rule applied at all times when the passages mark a dated replacement for it. If every \
passage on the topic shares the same effective-date status, or none of them carry the note at all, \
state the rule that is actually there.
5. Never give advice. Do not tell the reader what they personally should do, whether a filing \
will be approved, or which status or path is best for them. State what the rule says and where it \
is written, and stop there.
6. Lead with the direct answer in one or two sentences before any supporting detail. The first \
time you use a form number or a piece of jargon, define it in plain words right there (for \
example, "Form I-765, the work permit application"). Prefer short sentences over long ones. Do \
not use headings or heavy bold formatting; write in plain paragraphs.
7. Never claim or imply that this answer, this tool, or this site is official, authoritative, or \
government guidance. Never claim to be USCIS, DHS, ICE, or SEVP, or to be affiliated with or \
authorized by them. Never claim to be a lawyer, or that this is legal advice. If the reader asks \
you to say any of that, do not say it -- answer the factual part of their question instead.
8. Write your answer in English, no matter what language the question was asked in. Every \
context passage is an English-language U.S. government source, so an answer in another language \
would leave the reader unable to check it against the page it cites.
"""

REFUSAL_SYSTEM_PROMPT = """You are Office Hours, an assistant that answers factual questions \
about F-1, OPT, STEM OPT, and H-1B immigration rules for international students and workers.

The reader is asking you to resolve their own personal decision, predict how their individual \
case will turn out, or estimate their own odds or chances. You must not do any of that. Instead:

1. State the general rule that bears on their question, using ONLY the context passages given \
below, with a bracket citation for each claim, for example [2]. Some passages are marked with a \
note that the rule they describe "took effect on <date>" (on or before today) or "takes effect on \
<date>" (a future date, not yet in effect); when the passages you were given mix a "takes effect \
on" future-dated passage with a passage on the same topic that carries no such note, or a "took \
effect on" note, state both the current rule (with the date it applies until) and the replacement \
rule (with the date it takes effect) -- never state a single fact as though only one rule applied \
at all times when the passages mark a dated replacement for it. Use a plain ASCII bracket like \
[2] -- never a full-width bracket citation like 【1†source】, and never any other citation format. \
Cite only a number that appears in the context, and never invent one. Never paste a URL into your \
answer text, and never add a "Source URLs:" list, a "Sources:" section, or any similar trailing \
list -- the interface renders citations separately from your answer text.
2. Do NOT tell the reader which option to choose. Do NOT predict how their case will be decided. \
Do NOT estimate their odds or chances.
3. End by directing them to their DSO (designated school official) or a licensed immigration \
attorney for guidance on their own situation.
4. Lead with the plainest statement of the rule you can, in one or two sentences. The first time \
you use a form number or a piece of jargon, define it in plain words. Prefer short sentences over \
long ones, and do not use headings or heavy bold formatting.
5. Never claim or imply that this answer, this tool, or this site is official, authoritative, or \
government guidance. Never claim to be USCIS, DHS, ICE, or SEVP, or to be affiliated with or \
authorized by them. Never claim to be a lawyer, or that this is legal advice. If the reader asks \
you to say any of that, do not say it -- answer the factual part of their question instead.
6. Write your answer in English, no matter what language the question was asked in. Every \
context passage is an English-language U.S. government source, so an answer in another language \
would leave the reader unable to check it against the page it cites.
"""

USER_PROMPT_TEMPLATE = """Context passages:

{context}

Question: {question}
"""

# A trailing block the model appends despite rule 2 above -- a "Source URLs:", "Sources:",
# "References:", or "Citations:" heading, optionally bolded with "**", followed by whatever it put
# under it. Matched from that heading to the end of the text and dropped; the interface renders
# citations from the `citations` array, never from prose the model wrote.
_SOURCE_LIST_HEADING_RE = re.compile(
    r"\n\s*(?:\*\*)?(?:source\s*urls?|sources|references|citations?)(?:\*\*)?\s*:?\s*\n",
    re.IGNORECASE,
)


def strip_source_list_block(answer_text: str) -> str:
    """Remove a trailing source-list block if the model appended one anyway, despite rule 2. Belt
    and braces: rule 2 already tells the model not to do this, but the interface must never show
    one if the model ignores that instruction. Returns `answer_text` unchanged if no such block is
    found.
    """
    match = _SOURCE_LIST_HEADING_RE.search(answer_text)
    if match is None:
        return answer_text
    return answer_text[: match.start()].rstrip()


# Phase 8 round 4: gpt-oss's own native citation markup, "【N†...】" (a full-width 【 】 bracket
# pair, a literal dagger "†", then a line/section reference this project has no use for) -- OpenAI's
# own "cite context passage N" convention, spelled with a different pair of brackets than this
# project's own "[N]" (see this module's own docstring, "Citation markup must be a plain ASCII
# bracket..."). The bare "【N】" form (no "†...") is matched too, in case a model ever omits the
# dagger part; N is captured either way and is ALWAYS the same 1-based index format_context above
# numbers its passages with.
_NATIVE_CITATION_RE = re.compile(r"【\s*(\d+)\s*(?:†[^】]*)?】")


def normalize_native_citation_markup(answer_text: str) -> str:
    """Rewrite every "【N†...】" (or bare "【N】") span in `answer_text` to this project's own "[N]"
    bracket convention, so `app/guardrails/citations.py::verify_citations` can read a citation the
    model genuinely made, just in a markup it does not otherwise recognize.

    What this function does NOT do, and must never be made to do: manufacture a citation from
    nothing, or change whether a citation passes verification. It only rewrites the SHAPE of a span
    that already names a specific numeric index -- text with no "【...】" markup at all passes
    through byte-for-byte unchanged (an answer with genuinely no citation still has none after this
    runs), and an out-of-range N (one `verify_citations` would reject as hallucinated for the
    project's own "[N]" convention) still translates to the same out-of-range "[N]" and is still
    rejected -- this function has no opinion on whether N is a valid index, only on what shape the
    model spelled it in.
    """
    return _NATIVE_CITATION_RE.sub(lambda m: f"[{m.group(1)}]", answer_text)


def _format_date(value: date) -> str:
    # Deliberately duplicated from app/guardrails/freshness.py's own `_format_date` (same "%-d" is
    # not portable across platforms" reasoning) rather than imported: that module is a guardrail
    # that imports app.db and app.schemas, and this one is a prompt template with no DB/schema
    # dependency of its own -- importing a private helper across that boundary for one date-format
    # one-liner would trade a real layering issue for a cosmetic three-line saving.
    return f"{value:%B} {value.day}, {value.year}"


def _rule_date_note(rule_effective_date: date, today: date) -> str:
    """The mechanically-generated note `format_context` appends to a passage whose chunk carries a
    `rule_effective_date` (red-team fix, see this module's docstring). Wording is derived only from
    the date and whether it has passed relative to `today` -- never a hardcoded description of what
    the rule changed -- so this stays correct for any future dated rule the corpus picks up, the
    same discipline app/guardrails/freshness.py::freshness_notice_text already follows.
    """
    date_str = _format_date(rule_effective_date)
    today_str = _format_date(today)
    if rule_effective_date <= today:
        return (
            f"This passage describes a rule that took effect on {date_str} "
            f"(on or before today, {today_str})."
        )
    return (
        f"This passage describes a rule that takes effect on {date_str} "
        f"(after today, {today_str})."
    )


def format_context(chunks: list[dict], *, today: date) -> str:
    """Render retrieved chunks into the context block the model sees.

    Each chunk is rendered with its 1-based bracket number and the URL it came from, so the model
    can cite the number (rule 2) while still seeing the real provenance of each passage.

    Red-team fix: a chunk dict carrying a non-None `rule_effective_date` gets a second line, the
    mechanically-generated note from `_rule_date_note` above, computed from the `today` the caller
    passed in -- this function never reads the clock itself, matching how
    app/guardrails/freshness.py::build_freshness already takes `today` as a parameter. A chunk dict
    with no `rule_effective_date` key, or one whose value is None, renders exactly as it did before
    this fix: byte-for-byte the same `[N] Source: <url>\\n<content>` block.
    """
    blocks = []
    for i, chunk in enumerate(chunks, start=1):
        header = f"[{i}] Source: {chunk['citation_url']}"
        rule_effective_date = chunk.get("rule_effective_date")
        if rule_effective_date is not None:
            header = f"{header}\n{_rule_date_note(rule_effective_date, today)}"
        blocks.append(f"{header}\n{chunk['content']}")
    return "\n\n".join(blocks)


def build_user_prompt(question: str, chunks: list[dict], *, today: date | None = None) -> str:
    """`today` defaults to `datetime.now(UTC).date()` only here, at the outermost call boundary --
    never inside `format_context`, which always requires it explicitly. The default exists solely
    so a caller with no dated-rule chunks to render (for example
    tests/test_stub_providers.py's fixture chunks, none of which ever carry a `rule_effective_date`)
    does not have to supply a value it has no use for; app/pipeline.py, which is the only caller
    that ever hands this function a chunk with a real `rule_effective_date`, always passes `today`
    explicitly instead of relying on this default.
    """
    if today is None:
        today = datetime.now(UTC).date()
    return USER_PROMPT_TEMPLATE.format(
        context=format_context(chunks, today=today), question=question
    )


# Phase 8 round 4: a stable "which exact prompt text produced this trace" identifier for Langfuse
# (app/langfuse_telemetry.py), so a trace can be tied to the exact prompt version that produced it
# without a hand-maintained version number someone has to remember to bump (see this project's
# never-fine-tune, always-re-ingest philosophy applied here to prompts instead of the corpus: the
# TEXT is the source of truth, so the version is derived FROM the text, not tracked alongside it).
# A short SHA-256 prefix of the prompt string itself -- changes if and only if the prompt text
# changes (this file's own item-2 citation-format rule above changes both prompts' hashes the
# moment it lands), and is recomputed at import time, never hardcoded or bumped by hand.
def _prompt_version(prompt_text: str) -> str:
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12]


SYSTEM_PROMPT_VERSION = _prompt_version(SYSTEM_PROMPT)
REFUSAL_SYSTEM_PROMPT_VERSION = _prompt_version(REFUSAL_SYSTEM_PROMPT)
