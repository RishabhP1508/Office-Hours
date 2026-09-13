# 0021: Run the non-Latin script gate before the clarifier's vague check

## Context

ADR 0018 placed the non-Latin-script no-answer gate (`app/pipeline.py::_is_predominantly_non_latin`)
at "Step 1.5, directly after the clarifier's own vague check" -- the clarifier ran first, and the
gate only ever saw a question the clarifier had already let through. This ADR supersedes ADR 0018
on ORDERING ONLY. ADR 0018's own decision -- gate a question to NO_ANSWER only when it is
predominantly non-Latin (a non-Latin letter present AND zero Latin content words anywhere), and let
a bare Latin anchor such as "STEM OPT" rescue a mixed-script question regardless of surrounding
script -- and the retrieval evidence behind it (the Cyrillic, Chinese, and Spanish "STEM OPT"
questions all retrieving the identical correct chunk set, at cosine distances 0.3461, 0.3313, and
0.3385) are unchanged and are explicitly NOT revisited here.

Both the gate and the clarifier's `is_too_vague` are pure functions of the question string alone --
neither touches the pool, the embedder, or any external state. That means the exact set of
questions whose outcome depends on which one runs first is computable directly from the two
predicates, not something that has to be guessed at or sampled for. It was computed, not estimated:
28 production probes were run, 7 non-Latin scripts (Hindi, Chinese, Korean, Arabic, Cyrillic,
Japanese, Thai) crossed with 4 question-density tiers (bare vague, bare short-but-specific, bare
long, and anchored), through the real pipeline, under the OLD ordering (clarify first).

**6 of 28 change under the swap.** These are the bare, short, non-Latin questions with no Latin
anchor at all -- for example a Korean "please help" or a two-to-three-word non-Latin question with
nothing an English speaker would recognize. Under the old order, `is_too_vague` saw these first,
found too little content (by content-word or content-character count, per
`app/guardrails/clarifier.py`), and returned CLARIFY / `refusal_reason="query_too_vague"`. That
message tells the reader their question is unclear. The real problem is not that the question is
unclear -- it is that this tool cannot read the script it is written in at all, which is a different
and more accurate thing to say. After the swap, the gate sees these first, and every one of them
already satisfies "non-Latin letter present, zero Latin content words," so they now return
NO_ANSWER / `refusal_reason="non_latin_script_unsupported"` instead.

**8 of 28 do not change: non-Latin questions that already had enough content to clear
`is_too_vague` on their own.** Seven of these are bare, long non-Latin questions (one per script);
the eighth is a bare, short non-Latin question that still had enough content characters or words to
fail `is_too_vague` under the old order (a scriptio-continua question just over the
content-character floor, for instance). All eight passed through the clarifier under the old order
and hit the gate immediately afterward anyway. Reordering which of two checks runs first cannot
change an outcome neither of them would have altered under the old order -- the gate would have
fired for these questions regardless.

**14 of 28 do not change: questions carrying a Latin anchor.** Any content word containing even one
Latin letter -- "OPT," "STEM OPT," "E-Verify," "F-1," and so on, embedded in an otherwise non-Latin
sentence -- makes `_is_predominantly_non_latin` return False unconditionally (this is ADR 0018's own
zero-anchor rule, untouched here). A question that never trips the gate reaches the clarifier
exactly as before, in either order, and the clarifier's own verdict on it is unaffected by which
check ran first, because the gate never returns for it at all.

**A further case, deliberately constructed rather than drawn from the 28 measured probes, because
none of the 28 happens to cross both conditions at once: a non-Latin question that carries a Latin
anchor AND is too vague at the same time** (for example a short question combining "OPT" with a
non-Latin word or two, phrased so the clarifier's own count still falls short). The gate declines to
fire on it, because of the anchor -- the same as any of the 14 anchored probes above -- so it falls
through to the clarifier's own vague check exactly as it did before, and must still return CLARIFY.
This is the interaction the reorder could plausibly have broken (if the gate's anchor rule and the
clarifier's own anchor-rescue rule disagreed on what counts as an anchor, for instance), so it is
asserted directly as its own test (`tests/test_guardrails.py::
test_non_latin_question_with_latin_anchor_and_too_few_content_words_still_clarifies`) rather than
assumed to follow from the 28 measured probes, none of which exercises it.

## Decision

Run the non-Latin script gate first; run the clarifier's vague check second. Concretely, swap the
two blocks inside `app/pipeline.py::answer_question`'s `classify` span: `_is_predominantly_non_latin`
now runs before `is_too_vague`. Neither predicate, threshold, message constant, response type, or
refusal reason changes. Both span attributes (`non_latin_script_gate_triggered`,
`clarify_triggered`) are still set on every request that reaches this far, regardless of which path
returns first.

## Blast radius

**0 of 21 golden rows change, and that zero has no power.** The swap can only change the outcome for
a question where both predicates are even capable of firing in principle -- and `_is_predominantly_
non_latin` requires at least one non-Latin-script letter to be present at all. `eval/golden.jsonl`
contains zero rows with any non-Latin letter (all 21 questions are plain English, the same fact
`tests/test_guardrails.py`'s own corpus-trap comments already rely on for this whole area of the
code). So the golden set cannot exercise either predicate's non-Latin branch, and running the eval
against it before and after this change would show 0 rows differing either way -- not because the
change is safe, but because the golden set is structurally incapable of containing a question this
change could possibly affect. This is a proof from reading the predicate, not a passing test result,
and reporting "0 of 21 change" without this second half would be a false reassurance.

## What this does NOT fix

The 14 anchored questions in the measurement above still return a correct, cited answer written
entirely in English, to a reader who asked in Hindi, Chinese, Korean, Arabic, Cyrillic, Japanese, or
Thai. Getting past this gate (or the clarifier) is necessary for a coherent answer, not sufficient
for one in the reader's own language -- this project does not translate the generated answer, the
disclaimer, the DSO/attorney redirect, or the source labels (see ADR 0017's own closing note on this
point). Reordering these two checks changes nothing about that; it is a separate defect, not
addressed here.

## Alternatives considered

**Leave the ordering as ADR 0018 set it, and instead change the CLARIFY message to say something
softer about possibly being a script problem.** Rejected: CLARIFY's own message
(`CLARIFY_QUESTION`) is a single fixed constant shared by every vague question regardless of
language, and this task's own constraints forbid touching message constants. Softening it generically
would also make the message less accurate for the overwhelming majority of English CLARIFY cases it
already serves correctly, to fix a problem specific to one script condition.

**Merge the two checks into one combined predicate.** Rejected: the two checks answer different
questions ("is there enough content to retrieve against at all" vs. "is this a script this
corpus/embedder can retrieve against reliably") and are documented, tested, and owned separately
(`app/guardrails/clarifier.py` vs. `app/pipeline.py`'s STOPGAP block). Merging them would entangle
two independently-evolving pieces of logic for a change that is achievable as a pure reorder.

## Remove this when

This ordering only matters as long as the non-Latin gate (ADR 0018) exists at all. ADR 0018's own
"Remove this when" section already names the removal condition (cross-lingual retrieval good enough
that a distance threshold alone separates a real non-Latin question from an unanswerable one) and
the full list of things that come out together in that commit. When that happens, this ADR's
decision becomes moot along with it -- there is no separate cleanup this ADR adds on top.
