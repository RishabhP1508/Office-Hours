r"""One clarifying question for a query too vague to retrieve against at all.

Deliberately conservative: this must fire only on a query that genuinely has nothing to search on,
never on a real but short factual question (a false-positive clarify on a real question is worse
than a false negative, since it costs the person a round trip for no reason). It must never see
eval/golden.jsonl and is never tuned against it -- its own tests use vague and specific queries
written for the test file itself, not golden rows. `eval/golden.jsonl` is also entirely English, so
it is structurally incapable of exercising anything checked in this module at all (see the red-team
regression class in tests/test_guardrails.py for why that matters).

THE RULE, and why a bare token count cannot work across scripts:

For a SPACE-SEPARATED question (English, Hindi/Devanagari, Korean, Arabic, Russian/Cyrillic,
Spanish, and every other script that puts whitespace between words), the signal is the same one
this module always used: split on whitespace, strip leading/trailing punctuation from each chunk,
drop generic English stopwords, and count what is left. A question needs at least
`min_content_words` (Settings.CLARIFY_MIN_CONTENT_WORDS, default 3) of them to be considered
specific enough to retrieve against. This now tokenizes on whitespace rather than an ASCII-only
character class, which is the fix for the actual production defect (see RED-TEAM FIX below) --
Devanagari, Hangul, Arabic, and Cyrillic all separate their words with whitespace exactly like
English does, so splitting on whitespace and stripping surrounding punctuation tokenizes them
correctly with no script-specific logic at all, and the (English-only) stopword list simply never
matches a non-English token, which is fine: it only ever needs to strip English function words.

For a SCRIPTIO CONTINUA question -- one containing at least one character from a script that
writes running text with NO whitespace between words at all (Chinese and Japanese, written in Han
ideographs/Hiragana/Katakana, and Thai) -- whitespace tokenization cannot produce a word count,
because it does not produce a WORD boundary in the first place: the entire run of characters
between two spaces (often the entire question) is a single whitespace chunk regardless of how much
it says. Measured directly against the corpus's own real Chinese question before deciding this:
`我的实习工作许可可以延长多少个月？` (a genuine, fully-formed question, "how many months can my
work permit be extended for") is ONE whitespace-delimited chunk, so ANY word-count minimum above 1
rejects it, including one derived from a Unicode-aware tokenizer -- broadening the token character
class to `\w` does not help, because the character class is not what is missing; the SEPARATOR is.
So for a scriptio-continua question this module counts CONTENT CHARACTERS instead of content words:
every character in the question that is not whitespace and not punctuation/symbol (so a run of Han
ideographs, Hiragana, Katakana, Thai letters AND marks, or interleaved Latin letters/digits, all
count; a bare "？" does not). A question needs at least `min_content_chars`
(`_CLARIFY_MIN_CONTENT_CHARS`, default 10) of them.

Both numbers were checked against real measured examples, not picked from nowhere, following the
same discipline Settings.NO_ANSWER_MAX_DISTANCE's own comment in app/config.py uses (measure the
positive and negative controls, then place the threshold in the gap with margin on both sides,
never against eval/golden.jsonl -- which cannot even see this path, being entirely English):

    MUST NOT clarify (content-char count), the real production failures plus the module's own
    hand-written positive controls (see tests/test_guardrails.py):
        16  我的实习工作许可可以延长多少个月？          (Chinese, real production question)
        17  STEM OPT 延期可以延长多少个月？             (mixed-script, already-passing control)
        14  実務研修の延長は何か月ですか                  (Japanese, kanji + hiragana, no Latin)
        33  การขยายเวลา STEM OPT ใช้เวลากี่เดือน        (Thai)
    MUST clarify (content-char count), hand-written vague negative controls in the same scripts:
        2   帮助 / 签证                                  (Chinese "help" / "visa")
        6   我有一个问题                                  (Chinese "I have a question")
        3   ヘルプ    2  ビザ                             (Japanese "help" / "visa")
        8   ช่วยด้วย   5  วีซ่า                          (Thai "please help" / "visa")

The lowest real measurement is 14; the highest vague measurement is 8. `_CLARIFY_MIN_CONTENT_CHARS`
= 10 sits in that gap, 4 below the lowest real question and 2 above the highest vague one -- the
same shape of margin NO_ANSWER_MAX_DISTANCE keeps over its nearest off-topic control, chosen for the
same reason: a false-positive clarify on a real question is the worse failure, so the threshold
sits closer to the vague side of the gap than the real side.

There is no character-count equivalent of the anchor rescue below (a short-but-specific
scriptio-continua question containing a bare Latin anchor like "OPT" could, in principle, still be
below 10 total content characters and get wrongly clarified). This is a known, accepted asymmetry,
not an oversight: none of the five real production failures this fix targets are that short, and
inventing a rescue mechanism with no real failure to calibrate it against would be exactly the kind
of speculative logic this module's own docstring already warns against for the word-count case.

RED-TEAM FIX (2026-09-07): every question written in a non-Latin script was rejected as CLARIFY,
because `_TOKEN_RE` was `[a-z0-9]+(?:-[a-z0-9]+)*` -- an ASCII-only character class -- so a question
with zero ASCII letters always produced zero content words, unconditionally below
`CLARIFY_MIN_CONTENT_WORDS`, and the anchor rescue (which also needs an ASCII content word) could
not save it either. Five real, fully-formed production questions in Hindi (x2), Chinese, Korean,
and Arabic were all wrongly clarified, in a language the person could not have used to answer the
clarifying question anyway. Confirmed with a live regex probe before writing this fix (not assumed):
naively broadening the token pattern to Unicode word characters (`[^\W_]+`) does NOT fix this on its
own for Chinese/Japanese/Thai, and actively creates a NEW failure mode for Hindi: Python's `\w` does
not match Unicode combining marks (category Mn/Mc), which the Devanagari matra system depends on
(`ओपीटी` -- five characters, two of them combining vowel signs -- fragments into `['ओप', 'ट']` under
`[^\W_]+`, an artifact of where the regex engine treats a matra as a separator, not a real word
boundary). Splitting on WHITESPACE instead of a character class sidesteps that: Devanagari (and
Hangul, Arabic, Cyrillic) keep their matras attached to the syllable they belong to because nothing
about whitespace-splitting looks inside a chunk at all, and this is why the space-separated branch
above tokenizes on whitespace rather than a broadened character class.

Two known, accepted limitations from the same fix, both stated rather than silently carried:

    - The scriptio-continua character ranges checked below are Han ideographs (Chinese and
      Japanese Kanji), Hiragana, Katakana, and Thai -- exactly the three languages the corpus trap
      that motivated this fix named ("Chinese, Japanese and Thai do not put spaces between
      words"). Other scriptio-continua scripts (Lao, Khmer, Burmese) are not covered and would
      fall through to the word-count branch, where they would tokenize as one giant whitespace
      chunk and always clarify regardless of content -- the same failure this fix closes for
      Chinese/Japanese/Thai, just not extended to scripts nobody has reported yet.
    - This module does not, and cannot, control what LANGUAGE the downstream generator answers in,
      or translate the disclaimer, the DSO/attorney redirect, or the source labels -- getting past
      this gate is necessary, not sufficient, for a coherent answer in the asker's own language. See
      docs/adr/0017-non-latin-script-clarifier-fix.md for what was actually measured downstream.
"""

import unicodedata

# A conservative, generic English stopword list. Deliberately includes WH-question words ("how",
# "what", "when", "which", "who", "why") -- they carry no topic information on their own, and
# excluding them keeps queries like "How long is the STEM OPT extension?" counted on their real
# content words ("long", "stem", "opt", "extension") rather than being padded by the question's own
# grammar. Left English-only on purpose: a non-English content word never matches this list, so it
# is simply never stripped, which is the correct behavior (a Hindi or Arabic function word still
# counts toward specificity, since this module has no non-English stopword list to strip it with).
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
# guardrail exists to catch. English/Latin-script only (see module docstring, "known limitations")
# -- there is no character-count equivalent of this rescue.
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

# See module docstring, "THE RULE" -- calibrated from real measured examples, not picked from
# nowhere: the lowest real scriptio-continua question measured was 14 content characters, the
# highest vague one was 8; this sits in that gap, closer to the vague side.
_CLARIFY_MIN_CONTENT_CHARS = 10

# Unicode code point ranges for scripts that write running text with NO whitespace between words
# (see module docstring for why these three, specifically, need a different signal than a word
# count). Each entry is an inclusive (low, high) pair of code points.
_SCRIPTIO_CONTINUA_RANGES: tuple[tuple[int, int], ...] = (
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs (Chinese hanzi, Japanese kanji)
    (0x0E00, 0x0E7F),  # Thai
)

# Unicode general categories treated as punctuation/symbols for both `_strip_edges` (surrounding a
# whitespace-delimited word) and `_content_char_count` (the scriptio-continua fallback signal).
# Deliberately excludes every Mark category (Mn/Mc/Me) -- those are real letter content in scripts
# like Devanagari and Thai (combining vowel/tone signs), not decoration to discard; see the module
# docstring's account of the `[^\W_]+` regression this avoids.
_PUNCT_SYMBOL_CATEGORIES = frozenset(
    {"Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po", "Sm", "Sc", "Sk", "So"}
)

CLARIFY_QUESTION = (
    "Could you say a bit more about what you're asking -- for example, which visa or status "
    "(F-1, OPT, STEM OPT, H-1B), and what part of it (a form, a deadline, or a specific rule)?"
)


def _has_scriptio_continua_char(question: str) -> bool:
    """True if `question` contains at least one character from a script that separates words with
    no whitespace at all (see module docstring). Mixed-script input (a Latin loanword embedded in
    a Chinese sentence, for example) is still detected here, and handled correctly by
    `_content_char_count`, which counts content characters across the WHOLE string regardless of
    which script each one belongs to.
    """
    for ch in question:
        code_point = ord(ch)
        for low, high in _SCRIPTIO_CONTINUA_RANGES:
            if low <= code_point <= high:
                return True
    return False


def _content_char_count(question: str) -> int:
    """Count of characters in `question` that are neither whitespace nor punctuation/symbols --
    the scriptio-continua fallback signal (see module docstring, "THE RULE"). Letters, combining
    marks, and digits all count, in any script, so a mixed Latin+Han sentence like
    "STEM OPT 延期可以延长多少个月？" counts its Latin letters and its Han ideographs together.
    """
    count = 0
    for ch in question:
        if ch.isspace():
            continue
        if unicodedata.category(ch) in _PUNCT_SYMBOL_CATEGORIES:
            continue
        count += 1
    return count


def _strip_edges(token: str) -> str:
    """Strip leading and trailing punctuation/symbol characters from `token`, leaving letters,
    marks, and digits (including any internal hyphen or punctuation) untouched. Used instead of a
    character-class regex specifically so a Devanagari/Thai combining mark at the edge of a token
    is never mistaken for punctuation and dropped (see module docstring).
    """
    start, end = 0, len(token)
    while start < end and unicodedata.category(token[start]) in _PUNCT_SYMBOL_CATEGORIES:
        start += 1
    while end > start and unicodedata.category(token[end - 1]) in _PUNCT_SYMBOL_CATEGORIES:
        end -= 1
    return token[start:end]


def _content_words(question: str) -> list[str]:
    """Whitespace-tokenized content words with English stopwords and surrounding punctuation
    stripped (see module docstring, "THE RULE" -- the space-separated branch). Splitting on
    whitespace rather than a character class is what keeps this correct for Devanagari, Hangul,
    Arabic, and Cyrillic, none of which get any special-cased logic here: they tokenize correctly
    for the same reason English always has, because all four put whitespace between words.
    """
    words = []
    for raw in question.split():
        token = _strip_edges(raw).lower()
        if not token:
            continue
        if token in _STOPWORDS:
            continue
        words.append(token)
    return words


def is_too_vague(
    question: str,
    *,
    min_content_words: int = 3,
    min_content_chars: int = _CLARIFY_MIN_CONTENT_CHARS,
) -> bool:
    """True if `question` has too little content to retrieve against and should get the one
    clarifying question instead (see the module docstring for the exact rule and why).

    `min_content_chars` is not currently threaded from Settings (unlike `min_content_words`):
    app/pipeline.py's own call site is out of scope for this fix, so the scriptio-continua
    threshold is this function's own default rather than a configured value -- see the module
    docstring's calibration section for why 10 was chosen.
    """
    if _has_scriptio_continua_char(question):
        return _content_char_count(question) < min_content_chars
    words = _content_words(question)
    if len(words) >= min_content_words:
        return False
    if len(words) >= _ANCHOR_RESCUE_MIN_WORDS and any(w in _DOMAIN_ANCHOR_TOKENS for w in words):
        return False
    return True
