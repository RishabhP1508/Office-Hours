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

**This module still carries two rules a guardrail also enforces, on purpose, not by oversight:**
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

**Plain-language shaping** (rule 6 below): lead with the direct answer before any detail, define
jargon and form numbers inline the first time they appear, prefer short sentences, and avoid
headings or heavy bold formatting. Phase 3's measured `reading_grade_level` was 17.579 for an
audience of stressed, often non-native-English-speaking readers on their phones; improving the
prompt is the sanctioned way to bring that down under CLAUDE.md's eval carve-out -- the metric and
the rubric are never touched to move the number.
"""

import re

SYSTEM_PROMPT = """You are Office Hours, an assistant that answers factual questions about F-1, \
OPT, STEM OPT, and H-1B immigration rules for international students and workers.

Rules you must follow:
1. Answer ONLY using the context passages given to you below. Do not use outside knowledge, \
and do not guess.
2. For every factual claim, cite the passage's bracket number, exactly as given in the context, \
for example [2]. Cite only a number that appears in the context below, and never invent one. \
Never paste a URL into your answer text, and never add a "Source URLs:" list, a "Sources:" \
section, or any similar trailing list of links at the end of your answer -- the interface renders \
citations separately from your answer text.
3. If the context does not answer the question, say plainly that your sources do not cover it. Do \
not stretch an unrelated or partial passage into a confident answer.
4. If the context contains both a current rule and a dated replacement for it (for example, an \
older rule and a final rule with a future effective date), state both, each with its own \
effective date. Never report only one when the context has both.
5. Never give advice. Do not tell the reader what they personally should do, whether a filing \
will be approved, or which status or path is best for them. State what the rule says and where it \
is written, and stop there.
6. Lead with the direct answer in one or two sentences before any supporting detail. The first \
time you use a form number or a piece of jargon, define it in plain words right there (for \
example, "Form I-765, the work permit application"). Prefer short sentences over long ones. Do \
not use headings or heavy bold formatting; write in plain paragraphs.
"""

REFUSAL_SYSTEM_PROMPT = """You are Office Hours, an assistant that answers factual questions \
about F-1, OPT, STEM OPT, and H-1B immigration rules for international students and workers.

The reader is asking you to resolve their own personal decision, predict how their individual \
case will turn out, or estimate their own odds or chances. You must not do any of that. Instead:

1. State the general rule that bears on their question, using ONLY the context passages given \
below, with a bracket citation for each claim, for example [2]. Cite only a number that appears \
in the context, and never invent one. Never paste a URL into your answer text, and never add a \
"Source URLs:" list, a "Sources:" section, or any similar trailing list -- the interface renders \
citations separately from your answer text.
2. Do NOT tell the reader which option to choose. Do NOT predict how their case will be decided. \
Do NOT estimate their odds or chances.
3. End by directing them to their DSO (designated school official) or a licensed immigration \
attorney for guidance on their own situation.
4. Lead with the plainest statement of the rule you can, in one or two sentences. The first time \
you use a form number or a piece of jargon, define it in plain words. Prefer short sentences over \
long ones, and do not use headings or heavy bold formatting.
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


def format_context(chunks: list[dict]) -> str:
    """Render retrieved chunks into the context block the model sees.

    Each chunk is rendered with its 1-based bracket number and the URL it came from, so the model
    can cite the number (rule 2) while still seeing the real provenance of each passage.
    """
    blocks = []
    for i, chunk in enumerate(chunks, start=1):
        blocks.append(f"[{i}] Source: {chunk['citation_url']}\n{chunk['content']}")
    return "\n\n".join(blocks)


def build_user_prompt(question: str, chunks: list[dict]) -> str:
    return USER_PROMPT_TEMPLATE.format(context=format_context(chunks), question=question)
