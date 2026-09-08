"""Programmatic guard against the tool claiming to be official, authoritative, or a lawyer.

app/prompts.py's rule 7 (SYSTEM_PROMPT) and rule 5 (REFUSAL_SYSTEM_PROMPT) already tell the model
never to do this. This module is the belt-and-braces backstop for a model that ignores that
instruction anyway -- the same relationship app/guardrails/citations.py has to prompts.py's rule 2,
and app/guardrails/clarifier.py has to nothing (see that module's own docstring for why it exists
standalone). A model asked, directly or by a prompt-injected "SYSTEM:" line inside the user's own
question, to "confirm this is official USCIS guidance" will sometimes just do it, and the sentence
that does it is short enough that services/frontend/lib/prose.ts::deriveLead promotes it to the
largest text on the page -- directly above a disclaimer saying the opposite. Citation verification
(app/guardrails/citations.py) does not catch this: it checks that bracket indices resolve to
retrieved context, not what the surrounding prose asserts about itself, and a sentence like "This
answer reflects official USCIS guidance." sits perfectly well next to a correctly cited claim.

DETECTION RULE, exactly as implemented below, and why it is shaped this way:

A sentence trips when it contains a SUBJECT (a phrase referring to this answer or this speaker) and
an AUTHORITY PREDICATE (a phrase asserting officialness, government affiliation, or legal
authority), with the predicate occurring somewhere after the subject in the same sentence, and NO
NEGATION anywhere in the character span between the start of the subject match and the start of the
predicate match. This is a per-sentence, per-(subject, predicate)-pair rule: a sentence trips if ANY
such pair exists with nothing negating it in between, even if other pairs in the same sentence are
negated. Sentence splitting is a simple `[.!?]`-plus-whitespace split -- adequate for the short,
plain-paragraph answers this project's prompts already require (see prompts.py rule 6), not a full
sentence tokenizer.

    SUBJECT:    this answer, this response, this guidance, this information, this tool, this site,
                this is, these answers, I am, I'm, we are, we're, as your, speaking as.
    NEGATION:   not, n't, never, no, unofficial, cannot, unable, nor, neither.

Round 4 (red-team measurement, worst finding yet): `cannot`, `unable`, `nor`, `neither` were missing
from NEGATION. "This tool cannot give legal advice." -- exactly the sentence prompts.py rule 7
actively tells the model to write -- tripped the guard, because "not" does not match inside the
fused word "cannot" (no word boundary between "can" and "not" -- both word characters, same gap
that would break `\bofficial\b` matching inside `unofficial` if that boundary were missing there).
9 of 12 real denial-of-authority sentences tripped before this fix; see
`tests/test_guardrails.py`'s "authority denials" test class for the full list. This was the worst
false positive found across all four rounds: it punishes the model for obeying the rule this
project just added, and the message shown to the user (a citation-style "did not pass") says the
opposite of what actually happened. Note also why neither eval/golden.jsonl nor eval/results/*.json
could have caught this class: every stored generated answer predates rule 7's existence, so none of
them contains a rule-7-style denial at all -- the corpus is structurally incapable of exercising
this failure mode, not merely silent about it (the same shape of gap as round 1's zero occurrences
of "official" in golden.jsonl).

Round 2 (red-team verification of round 1): `official` and `officially` as BARE, standalone
predicates were too weak. "a school official", "the official end date", "the official selection
pool" are ordinary descriptive English, not an authority claim, and with a subject anywhere earlier
in the sentence (very common: "As a STEM OPT student, ... your designated school official ...",
"This is a question for your designated school official") a bare `official` tripped 8 of 10
real-shaped, correct sentences. `official`/`officially` are no longer standalone predicates at all;
they only count as an AUTHORITY PREDICATE in one of two tightly-scoped shapes (PREDICATES, tier 2
below). This is why `as a` was also dropped from SUBJECT above: it triggers on an ordinary,
extremely common domain phrase ("As a STEM OPT student...", "As a beneficiary...", "As a
rule..."), it was not needed by any real true-positive case (every true positive here pairs with
`this answer`, `this is`, `I am`, `we are`, or `as your` instead), and removing it costs nothing
while shrinking the false-positive surface. `this is` COULD NOT be dropped the same way even
though it is just as common: "Yes, this is official USCIS guidance." and "This is legal advice."
have no OTHER subject phrase in the sentence, so dropping `this is` would silently un-catch them.
Both decisions were checked against the full true-positive and false-positive test data before
deciding, not assumed.

    PREDICATES, tier 1 (strong -- each its own label for the span attribute; see
    `verify_no_authority_claim` for why a label and never the matched text):
        authoritative                                                     -> "authoritative"
        authorized by                                                     -> "authorized_by"
        affiliated with                                                   -> "affiliated_with"
        on behalf of                                                      -> "on_behalf_of"
        (USCIS|DHS|ICE|SEVP|government) guidance                         -> "agency_guidance"
        (USCIS|DHS|ICE|SEVP) (officer|official|tool|bot|representative|agency) -> "agency_role"
        legal advice                                                      -> "legal_advice"
        legal opinion                                                     -> "legal_opinion"
        your (attorney|lawyer|counsel), up to 2 words apart               -> "attorney_persona"
    Note "government" is deliberately absent from the agency_role compound (unlike agency_guidance):
    "a government official" is ordinary phrasing for a generic public employee, unlike "a USCIS
    official" / "a DHS official", which in this domain reads as a persona claim. "counsel" and the
    up-to-2-word gap in the last row are round 3 (see that section below).

    PREDICATES, round 3 (a first-person ROLE CLAIM via "as a/an ...", self-contained -- see
    `_role_claim_match`, and "why not just restore `as a` as a SUBJECT" below):
        as a/an (USCIS|DHS|ICE|SEVP) (officer|official|representative|employee|adjudicator)
                                                                           -> "agency_role"
        as a/an (lawyer|attorney|counsel)                                 -> "attorney_persona"
    "As a USCIS officer, I can confirm the cap is 85,000 [1]." and "As a USCIS officer and can
    confirm this." assert the identical claim as "I am a USCIS officer and can confirm this." (a
    tier-1 true positive since round 1), but round 2 removed `as a` from SUBJECT entirely (it was
    the direct cause of 8 real false positives: paired with a bare `official` anywhere later in the
    sentence, "as a" also matched "As a STEM OPT student, ... designated school official"), so
    nothing was left to pair "as a" with a role predicate at all -- this was a real gap, caught by
    red-team verification, not a hypothetical. The fix is NOT restoring `as a` to SUBJECT: these two
    patterns bind "as a/an" directly, with zero gap, to a role noun, so the match can only ever be a
    real role claim -- there is no room in the pattern for an unrelated noun phrase ("STEM OPT
    student") to occupy the space between them. Checked again afterward against golden.jsonl, every
    stored eval answer, and this module's own false-positive test set: "As a STEM OPT student, ...
    designated school official" still passes, because "STEM" is not one of the five role nouns.

    PREDICATES, tier 2 (weak -- `official`/`officially`, only in two shapes, never bare):
    (a) "official_guidance": official/officially binds to an AUTHORITY-BEARING HEAD NOUN within a
        few words -- guidance, advice, policy, position, statement, determination, ruling,
        interpretation. "official USCIS guidance" trips; "official USCIS page", "official end
        date", "official selection pool" do not, because page/end date/selection pool are not in
        that noun list. This is what makes "official USCIS guidance" a claim regardless of context:
        claiming content IS official guidance/policy/advice is inherently an authority claim, not a
        descriptive one, the way "a school official" or "the official end date" are.
    (b) "official_self_claim": a DIRECT copular assertion -- one of `this answer`, `this response`,
        `this tool`, `this site`, `this information`, `this guidance`, `these answers` followed
        immediately (only whitespace between) by `is`/`are`, or the already-copular `I am`, `I'm`,
        `we are`, `we're` -- followed immediately (only whitespace) by `official`/`officially`.
        "This tool is official." and "This information is officially accurate." trip. Bare `this
        is` is deliberately EXCLUDED from this shape (unlike tier 1/2a pairing, where `this is`
        stays a valid SUBJECT): "This is officially the last day you may remain in the United
        States." must not trip, and "this is officially <anything>" is indistinguishable at the
        string level from "this is officially <a claim about the tool>" unless the subject names
        the answer/tool specifically. The tight, whitespace-only adjacency this shape requires is
        also what makes it self-negating without a separate check: "This tool is NOT official."
        does not match the shape at all, since a real word sits between `is` and `official`.

Word boundaries matter and are enforced throughout: `\bofficial\b` must NOT match inside
`unofficial` (the product's own disclaimer says "This is an unofficial tool", and `official` is a
substring of that word). Get the boundary wrong and the disclaimer's own wording would trip the
guard it is supposed to pass.

`licensed immigration attorney` and `licensed attorney` are deliberately NOT predicates. The model
writes the DSO/attorney redirect (prompts.py rule 3 in REFUSAL_SYSTEM_PROMPT, and
app/pipeline.py::_DSO_REDIRECT_SENTENCE) constantly, and matching it would block every correct
refusal. `your attorney` / `your lawyer` / `your counsel` (the model claiming to BE the reader's
attorney) are predicates; `a licensed immigration attorney` (redirecting the reader to one) is not
-- the up-to-2-word gap the `your (attorney|lawyer|counsel)` pattern allows (round 3, for "your
immigration counsel") still requires the phrase to START with `your`, so it does not turn "a
licensed immigration attorney" into a match no matter how the gap is sized.

PRIVACY (round 2): `verify_no_authority_claim` never returns the sentence that tripped it, only a
fixed, closed-vocabulary label (`AUTHORITY_PREDICATE_LABELS`) naming which predicate pattern fired.
This runs on exactly the path where a user's own question may have manipulated the model (a
prompt-injected "SYSTEM:" line), and this project stores no record of who asked what; the matched
sentence is still model-generated prose, however tightly patterned, and the fix is to never let it
leave this function at all rather than to redact it downstream. See app/pipeline.py's call site for
what it does with the label instead.

KNOWN MISSES (round 3): this is a PATTERN guard, not a semantic one. It catches an assertive,
English, first-person claim written in one of the specific shapes above; it does not understand
meaning, does not read another language, and does not follow implication or paraphrase. Recorded
here honestly, on purpose, rather than chased with more patterns -- see each category's own comment
for why it is out of scope for this fix, not merely unfinished:

    hedge-then-claim    "This answer is not a substitute for legal advice, and it reflects
                        official USCIS guidance." -- documented in detail below; the negation in
                        the hedge sits in the span this rule checks, vetoing the real claim after
                        it. Splitting on clauses was considered and rejected (see below).
    semantic paraphrase "You can treat this as the government's position on the matter [1]." --
                        asserts the same authority without any SUBJECT or PREDICATE string this
                        module recognizes at all.
    implied authority   "This carries the full weight of federal immigration policy [1]." --
                        same: no recognized SUBJECT or PREDICATE string.
    indirect role       "I work for USCIS and the cap is 85,000 [1]." -- "I work for" is not "I
                        am", and is not a SUBJECT.
    passive voice       "The following is officially sanctioned guidance from USCIS [1]." -- "the
                        following" is not a recognized SUBJECT ("this answer"/"this is"/etc.).
    non-English         "Esta es una guia oficial de USCIS. The cap is 85,000 [1]." -- every
                        pattern here is English-only by design. A Spanish or French sentence making
                        the identical claim renders today. This is DELIBERATELY not fixed here: it
                        reaches generation because the sentence is still Latin-script (a
                        Devanagari or Chinese question is rejected earlier, for an unrelated
                        reason -- the non-Latin-script tokenizer gap this project has separately,
                        already sequenced work on). Multilingual pattern matching belongs with
                        that work, not bolted onto this guard as an afterthought.

None of the six categories above gets a new pattern. A guard tuned until one constructed evasion
list reads green proves the list is green, not that the guard understands anything -- the whole
point of writing these down is so a future reader sees the real boundary of what this module does,
instead of mistaking six passing test cases for six closed gaps.

KNOWN LIMITATION (hedge-then-claim, detailed): a hedge-then-claim sentence such as "This answer is
not a substitute for legal advice, and it reflects official USCIS guidance." is a miss. The
negation ("not") sits in the span between the subject ("this answer") and every predicate later in
the same sentence (both "legal advice" and "official_guidance"), so it vetoes a pair that, read as a
whole sentence, plainly does make the claim after the hedge. Splitting on clauses (not just
sentences) was considered and rejected: the only subject that would catch the second clause on its
own is a bare "it", and "It is official USCIS policy that employers must ..." is a legitimate,
non-claiming sentence that a bare-`it` subject would wrongly block, and it would also fire on many
otherwise-fine answers that use "it" for something else entirely a sentence or two after a hedge.
`it` is deliberately NOT in the SUBJECT list above.
"""

import re

from app.guardrails.citations import VerificationResult

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# "as a" removed in round 2 (see module docstring): not needed by any true positive, and it is an
# extremely common, purely descriptive sentence-opener in this domain ("As a STEM OPT student...").
_SUBJECT_PATTERNS = (
    r"this\s+answer",
    r"this\s+response",
    r"this\s+guidance",
    r"this\s+information",
    r"this\s+tool",
    r"this\s+site",
    r"this\s+is",
    r"these\s+answers",
    r"i\s+am",
    r"i'm",
    r"we\s+are",
    r"we're",
    r"as\s+your",
    r"speaking\s+as",
)
_SUBJECT_RE = re.compile(r"\b(?:" + "|".join(_SUBJECT_PATTERNS) + r")\b", re.IGNORECASE)

# Tier 1 (strong): unchanged from round 1. Each pattern carries its own canonical label -- the ONLY
# thing verify_no_authority_claim ever returns for a match, never the matched text (see PRIVACY
# above). Order does not matter for matching (each is matched independently, see
# _labeled_predicate_matches below), but is kept in the same order as the module docstring's table.
_TIER1_LABELED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("authoritative", r"authoritative"),
    ("authorized_by", r"authorized\s+by"),
    ("affiliated_with", r"affiliated\s+with"),
    ("on_behalf_of", r"on\s+behalf\s+of"),
    ("agency_guidance", r"(?:uscis|dhs|ice|sevp|government)\s+guidance"),
    (
        "agency_role",
        r"(?:uscis|dhs|ice|sevp)\s+(?:officer|official|tool|bot|representative|agency)",
    ),
    ("legal_advice", r"legal\s+advice"),
    ("legal_opinion", r"legal\s+opinion"),
    # Round 3: "counsel" added alongside attorney/lawyer ("Speaking as your immigration counsel"
    # was a real miss). Allows up to 2 gap words between "your" and the role noun (not a strict
    # zero-gap "your attorney") specifically so "your immigration counsel" and "your immigration
    # attorney" match -- both are ordinary real phrasings of the same persona claim "your attorney"
    # already covers, not a new category. Checked against golden.jsonl, every stored eval answer,
    # and this module's own false-positive test set before keeping the gap (see
    # tests/test_guardrails.py): "a licensed immigration attorney" and "your DSO or a licensed
    # immigration attorney" (the real product redirect) both still pass, because "your" is never
    # within 2 words of attorney/lawyer/counsel in either phrase.
    ("attorney_persona", r"your\s+(?:[a-z]+\s+){0,2}(?:attorney|lawyer|counsel)"),
)
_TIER1_LABELED_RES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (label, re.compile(r"\b(?:" + pattern + r")\b", re.IGNORECASE))
    for label, pattern in _TIER1_LABELED_PATTERNS
)

# Tier 2a ("official_guidance"): official/officially binds to an authority-bearing head noun within
# up to 3 intervening words (covers "official USCIS guidance", "official U.S. government policy").
# A lookahead keeps the match itself just the official/officially token (so its .start() is where
# the word actually begins, exactly like every tier-1 match), while still requiring the head noun
# to follow -- "official USCIS page" never matches because "page" is not in this noun list.
_TIER2_HEAD_NOUNS = (
    "guidance",
    "advice",
    "policy",
    "position",
    "statement",
    "determination",
    "ruling",
    "interpretation",
)
_TIER2_HEADNOUN_RE = re.compile(
    r"\bofficial(?:ly)?\b(?=(?:\s+[A-Za-z][\w'-]*){0,3}\s+(?:"
    + "|".join(_TIER2_HEAD_NOUNS)
    + r")\b)",
    re.IGNORECASE,
)

# Tier 2b ("official_self_claim"): a direct copular assertion about the answer/tool itself. The
# named group "predicate" captures just the official/officially token, so its position -- not the
# subject phrase's -- is what gets fed into the same subject/negation pairing loop every other
# predicate goes through (see module docstring, shape (b), for why bare "this is" is excluded here).
_TIER2_SELF_NOUN_SUBJECTS = (
    r"this\s+answer",
    r"this\s+response",
    r"this\s+tool",
    r"this\s+site",
    r"this\s+information",
    r"this\s+guidance",
    r"these\s+answers",
)
_TIER2_COPULAR_RE = re.compile(
    r"\b(?:(?:"
    + "|".join(_TIER2_SELF_NOUN_SUBJECTS)
    + r")\s+(?:is|are)|i\s+am|i'm|we\s+are|we're)\s+(?P<predicate>official(?:ly)?)\b",
    re.IGNORECASE,
)

# Round 3: a first-person ROLE CLAIM made with "as a/an ..." rather than "I am ...". Deliberately
# NOT implemented by restoring "as a"/"as an" to SUBJECT (that was round 2's actual false-positive
# cause: paired with a bare `official` anywhere later in the same sentence, "as a" caught "As a
# STEM OPT student, ... designated school official" and 7 others like it). Instead, each of these
# is its OWN tightly-scoped, self-contained pattern that binds "as a/an" DIRECTLY to a role noun --
# it can only ever match a real role claim ("as a USCIS officer", "as an attorney"), never an
# unrelated later noun phrase, because there is no gap between "as a/an" and the role noun for
# anything else to occupy. The named group "role" is what the negation check below measures up to,
# the same "no negation between the claim's start and its predicate" rule every other match uses.
_ROLE_CLAIM_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "agency_role",
        r"as\s+an?\s+(?:uscis|dhs|ice|sevp)\s+(?P<role>officer|official|representative|employee|adjudicator)",
    ),
    ("attorney_persona", r"as\s+an?\s+(?P<role>lawyer|attorney|counsel)"),
)
_ROLE_CLAIM_RES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (label, re.compile(r"\b" + pattern + r"\b", re.IGNORECASE))
    for label, pattern in _ROLE_CLAIM_PATTERNS
)

# Round 4: cannot/unable/nor/neither added (see module docstring's "Round 4" paragraph). `cannot`
# needs its own `\bcannot\b` -- `\bnot\b` cannot match inside it, since "can" and "not" are fused
# with no word boundary between them (the same class of gap that makes `\bofficial\b` correctly
# skip over `unofficial`, just working against detection here instead of for it).
_NEGATION_PATTERNS = (
    r"\bnot\b",
    r"n't\b",
    r"\bnever\b",
    r"\bno\b",
    r"\bunofficial\b",
    r"\bcannot\b",
    r"\bunable\b",
    r"\bnor\b",
    r"\bneither\b",
)
_NEGATION_RE = re.compile(r"(?:" + "|".join(_NEGATION_PATTERNS) + r")", re.IGNORECASE)

# The closed, fixed vocabulary verify_no_authority_claim's `detail` is ever drawn from -- exported
# so tests (and any future caller) can assert membership without hand-maintaining a second copy of
# this set. Built from the tier-1 table plus the two tier-2 shapes, never hand-typed twice.
AUTHORITY_PREDICATE_LABELS: frozenset[str] = frozenset(
    {label for label, _ in _TIER1_LABELED_PATTERNS} | {"official_guidance", "official_self_claim"}
)


def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s]


def _labeled_predicate_matches(sentence: str) -> list[tuple[int, str]]:
    """Every (position, label) pair for an AUTHORITY PREDICATE found anywhere in `sentence`,
    across both tiers, sorted by position. `position` is always where the matched word itself
    starts (never a subject's position), so it plugs directly into the same span-based negation
    check every predicate goes through in `_sentence_claims_authority`.
    """
    matches = [
        (m.start(), label) for label, regex in _TIER1_LABELED_RES for m in regex.finditer(sentence)
    ]
    matches += [(m.start(), "official_guidance") for m in _TIER2_HEADNOUN_RE.finditer(sentence)]
    matches += [
        (m.start("predicate"), "official_self_claim") for m in _TIER2_COPULAR_RE.finditer(sentence)
    ]
    matches.sort(key=lambda pair: pair[0])
    return matches


def _role_claim_match(sentence: str) -> str | None:
    """The label of the first "as a/an <role>" first-person role claim found in `sentence` with no
    negation between "as" and the role noun, or None. Self-contained: unlike every other predicate
    here, this never consults `_SUBJECT_RE` at all -- "as a"/"as an" fuses its own subject and
    predicate into one tight phrase, which is exactly what keeps it from re-matching an unrelated
    later noun phrase the way the round-1 bare `official` + general "as a" subject did (see module
    docstring, round 3).
    """
    for label, regex in _ROLE_CLAIM_RES:
        match = regex.search(sentence)
        if match is None:
            continue
        span = sentence[match.start() : match.start("role")]
        if _NEGATION_RE.search(span) is None:
            return label
    return None


def _sentence_claims_authority(sentence: str) -> str | None:
    """The label of the first (subject, predicate) pair found -- predicate strictly after subject,
    no negation in the span between them -- or None if no such pair exists. See module docstring
    for the exact rule and the tier 1 / tier 2 split. Also checks the self-contained "as a/an
    <role>" claim (round 3, `_role_claim_match`), which needs no separate subject match at all.
    """
    subject_matches = list(_SUBJECT_RE.finditer(sentence))
    if subject_matches:
        predicate_matches = _labeled_predicate_matches(sentence)
        for subject_match in subject_matches:
            for predicate_start, label in predicate_matches:
                if predicate_start <= subject_match.start():
                    continue
                span = sentence[subject_match.start() : predicate_start]
                if _NEGATION_RE.search(span) is None:
                    return label
    return _role_claim_match(sentence)


def verify_no_authority_claim(answer_text: str) -> VerificationResult:
    """Pure function: no network, no DB, no model, no import of app.config. Scans `answer_text`
    sentence by sentence for the SUBJECT + AUTHORITY PREDICATE pattern described in the module
    docstring. On the first sentence that trips, returns `ok=False`,
    `reason="answer_claims_official_authority"`, and `detail` set to a canonical label from
    `AUTHORITY_PREDICATE_LABELS` naming which predicate pattern fired -- NEVER the sentence, NEVER
    any substring of the model's own generated text (see PRIVACY in the module docstring). Returns
    `ok=True` if no sentence trips.
    """
    for sentence in _split_sentences(answer_text):
        label = _sentence_claims_authority(sentence)
        if label is not None:
            return VerificationResult(
                ok=False,
                reason="answer_claims_official_authority",
                detail=label,
            )
    return VerificationResult(ok=True, reason=None)
