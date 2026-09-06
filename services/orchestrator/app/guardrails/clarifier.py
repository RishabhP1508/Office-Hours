"""One clarifying question for a query too vague to retrieve against at all.

Deliberately conservative: this must fire only on a query that genuinely has nothing to search on,
never on a real but short factual question (a false-positive clarify on a real question is worse
than a false negative, since it costs the person a round trip for no reason). It must never see
eval/golden.jsonl and is never tuned against it -- its own tests use vague and specific queries
written for the test file itself, not golden rows.

Signal chosen: after stripping stopwords, count the remaining content words. A query needs at
least `min_content_words` (Settings.CLARIFY_MIN_CONTENT_WORDS, default 3) of them to be considered
specific enough to retrieve against. A query with fewer content words can still be rescued from
CLARIFY if it names a recognized domain-anchor token (a visa/status code, a form number, or a
topic noun this corpus covers) -- but ONLY once it has at least two content words already
(`_ANCHOR_RESCUE_MIN_WORDS`). A single bare word or acronym on its own ("OPT?", "STEM?") is still
too vague to search meaningfully even though the word itself is a recognized anchor: there is no
question attached to it (duration? eligibility? paperwork?), so it is treated the same as "help"
or "visa" rather than rescued. Two content words that include an anchor ("OPT deadline",
"I-20 lost") are judged specific enough to skip straight to retrieval.
"""

import re

# A conservative, generic English stopword list. Deliberately includes WH-question words ("how",
# "what", "when", "which", "who", "why") -- they carry no topic information on their own, and
# excluding them keeps queries like "How long is the STEM OPT extension?" counted on their real
# content words ("long", "stem", "opt", "extension") rather than being padded by the question's own
# grammar.
_STOPWORDS = frozenset(
    {
        "a",
        "am",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "just",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "please",
        "shall",
        "should",
        "so",
        "some",
        "that",
        "the",
        "there",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

# Recognized topic anchors: visa/status codes, form numbers, and topic nouns this corpus actually
# covers. Deliberately does NOT include the bare word "visa" or "status" themselves -- naming the
# category ("visa") without saying which one is exactly the kind of underspecified question this
# guardrail exists to catch.
_DOMAIN_ANCHOR_TOKENS = frozenset(
    {
        "f-1",
        "f1",
        "j-1",
        "j1",
        "m-1",
        "m1",
        "opt",
        "stem",
        "h-1b",
        "h1b",
        "i-20",
        "i20",
        "i-765",
        "i765",
        "i-983",
        "i983",
        "i-129",
        "i129",
        "sevis",
        "dso",
        "uscis",
        "ead",
        "cap-gap",
        "unemployment",
        "extension",
        "cap",
        "travel",
        "employer",
    }
)

# A single bare content word never counts as specific enough on its own, even if it happens to be a
# recognized anchor token ("OPT?", "STEM?") -- see the module docstring. The anchor rescue only
# applies once there are at least this many content words.
_ANCHOR_RESCUE_MIN_WORDS = 2

# Keeps hyphenated forms ("f-1", "i-20", "cap-gap") as single tokens instead of splitting on the
# hyphen, so they can be matched whole against _DOMAIN_ANCHOR_TOKENS.
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

CLARIFY_QUESTION = (
    "Could you say a bit more about what you're asking -- for example, which visa or status "
    "(F-1, OPT, STEM OPT, H-1B), and what part of it (a form, a deadline, or a specific rule)?"
)


def _content_words(question: str) -> list[str]:
    tokens = _TOKEN_RE.findall(question.lower())
    return [t for t in tokens if t not in _STOPWORDS]


def is_too_vague(question: str, *, min_content_words: int = 3) -> bool:
    """True if `question` has too little content to retrieve against and should get the one
    clarifying question instead (see the module docstring for the exact rule and why).
    """
    words = _content_words(question)
    if len(words) >= min_content_words:
        return False
    if len(words) >= _ANCHOR_RESCUE_MIN_WORDS and any(w in _DOMAIN_ANCHOR_TOKENS for w in words):
        return False
    return True
