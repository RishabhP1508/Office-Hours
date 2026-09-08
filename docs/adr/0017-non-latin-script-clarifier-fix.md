# 0017. Non-Latin-script clarifier fix: whitespace tokens, character-count fallback

## Context

A red-team pass against production (2026-09-07) found that every question written in a non-Latin
script was rejected as CLARIFY / `query_too_vague`, sub-1.5s, before retrieval ever ran:

```
एसटीईएम ओपीटी एक्सटेंशन कितने महीने का होता है?     clarify / query_too_vague
我的实习工作许可可以延长多少个月？                      clarify / query_too_vague
ओपीटी कितने महीने का होता है और मुझे कब आवेदन करना चाहिए?  clarify / query_too_vague
옵티 연장은 몇 개월인가요? 신청 서류는 무엇인가요?          clarify / query_too_vague
(Arabic, no Latin characters)                        clarify / query_too_vague
```

The control that isolated the cause: `STEM OPT 延期可以延长多少个月？` answered correctly, because
it happens to contain the Latin substring "STEM OPT". `app/guardrails/clarifier.py`'s content-word
counter tokenized with `_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")` -- an ASCII-only
character class. A question with zero ASCII letters always produced zero content words,
unconditionally below `CLARIFY_MIN_CONTENT_WORDS` (3), and the anchor rescue (which also needs an
ASCII content word) could not save it either.

Verified before writing a fix, not assumed: broadening the token pattern to Unicode word characters
(`[^\W_]+`) does not close this. Two live regex probes against Python 3.12's `re` module:

- Chinese: `我的实习工作许可可以延长多少个月` (a real, fully-formed question) tokenizes as ONE token
  under `[^\W_]+`, because Chinese writes running text with no whitespace between words at all --
  broadening the character class does not create a word boundary the regex engine is not already
  finding, because the separator is what is missing, not the character class.
- Hindi: `ओपीटी` (five characters, two of them dependent vowel signs) fragments into
  `['ओप', 'ट']` under `[^\W_]+`, because Python's `\w` does not match Unicode combining marks
  (category Mn/Mc), which the Devanagari matra system depends on for real word boundaries. This
  would have made the fix strictly worse for Hindi in some cases: a vague two-word Hindi query with
  internal matras (`चाहिए`, "need", fragments into three pieces on its own) could artificially clear
  a 3-token minimum that a real tokenization of the same two words would not.

## Decision

Two different signals, chosen by whether the question contains a script that separates words with
whitespace or not:

1. **Space-separated scripts** (English, Hindi/Devanagari, Korean/Hangul, Arabic, Russian/Cyrillic,
   Spanish, and any other script that puts whitespace between words): tokenize by splitting on
   whitespace, strip leading/trailing punctuation/symbol characters from each chunk (never marks --
   see the Hindi finding above), lowercase, drop the existing English-only stopword list, and count
   what remains. This is the same content-word-count rule the module always used, just tokenized on
   whitespace instead of an ASCII character class. Whitespace splitting is what sidesteps the
   Devanagari-matra regression: nothing about splitting on whitespace looks inside a chunk, so a
   combining mark never gets treated as a separator.
2. **Scriptio continua scripts** (Chinese and Japanese, written in Han ideographs/Hiragana/Katakana,
   and Thai): these write words with no separator at all, so no whitespace-based tokenizer can
   produce a word count for them. The fallback signal is a content-CHARACTER count instead: every
   character that is not whitespace and not punctuation/a symbol (letters, marks, and digits, in any
   script, so a mixed `STEM OPT 延期...` sentence counts its Latin letters and Han ideographs
   together). A question needs at least 10 content characters.

Both numbers (`min_content_words=3`, unchanged; `min_content_chars=10`, new) were checked against
real measured examples, not picked from nowhere -- the same discipline `NO_ANSWER_MAX_DISTANCE`
already uses in `app/config.py` (measure the positive and negative controls, place the threshold in
the gap with margin on both sides, never against `eval/golden.jsonl`, which is entirely English and
cannot even exercise this path):

```
MUST NOT clarify (content-char count):
    16  我的实习工作许可可以延长多少个月？          Chinese, real production question
    17  STEM OPT 延期可以延长多少个月？             mixed-script, already-passing control
    14  実務研修の延長は何か月ですか                  Japanese, kanji + hiragana, no Latin
    33  การขยายเวลา STEM OPT ใช้เวลากี่เดือน        Thai
MUST clarify (content-char count):
    2   帮助 / 签证                                  Chinese "help" / "visa"
    6   我有一个问题                                  Chinese "I have a question"
    3   ヘルプ    2  ビザ                             Japanese "help" / "visa"
    8   ช่วยด้วย   5  วีซ่า                          Thai "please help" / "visa"
```

The lowest real measurement is 14; the highest vague measurement is 8. 10 sits in that gap, closer
to the vague side (4 below the lowest real question, 2 above the highest vague one) -- a
false-positive clarify on a real question is the worse failure, the same reasoning the module's
existing word-count threshold already documents.

`min_content_chars` is a default argument on `is_too_vague`, not a `Settings` field:
`app/pipeline.py`'s call site was out of scope for this fix, so there is nowhere to thread a
configured value through in production. Adding an unused `Settings` field would have been dead
configuration.

## Downstream measurement (retrieval and generation, real corpus, real Ollama)

Getting past the clarifier is necessary, not sufficient. Measured directly against the live
216-chunk corpus (`nomic-embed-text`, hybrid RRF retrieval, local `qwen3.5-8k` generator), asking
the equivalent of "how many months is the STEM OPT extension" in five languages plus one control:

| question (paraphrase) | min distance | NO_ANSWER fires (0.50) | response_type | answer language |
| --- | --- | --- | --- | --- |
| Hindi, "STEM OPT" transliterated phonetically, no Latin | 0.5167 | yes | no_answer | n/a |
| Hindi, second phrasing, no Latin | 0.5273 | yes | no_answer | n/a |
| Chinese, describes the topic without naming "STEM OPT" | 0.5308 | yes | no_answer | n/a |
| Korean, "OPT" transliterated, asks about months + documents | 0.4548 | no (slips under) | answer | English (correctly says the retrieved passages do not answer this) |
| Arabic, describes the topic without naming "STEM OPT" | 0.5185 | yes | no_answer | n/a |
| Spanish, keeps "STEM OPT" as a Latin loanword | 0.3417 | no | answer | Spanish, correct and cited |
| Mixed Chinese + Latin "STEM OPT" (pre-existing control) | 0.3313 | no | answer | Chinese, correct and cited |

The pattern is clean and worth stating plainly: retrieval only finds the right chunk when the query
keeps "STEM OPT" as a literal Latin substring. Every query that instead transliterates the term
phonetically into the target script, or paraphrases the topic without the acronym at all, retrieves
the wrong chunks entirely (H-1B FAQ, M-2 visa status, AUD/Form I-20 questions -- none of them STEM
OPT), at a distance above 0.50. This is a real cross-lingual retrieval gap in `nomic-embed-text`
against this narrow, acronym-heavy corpus, not a near-miss around the threshold: the correct chunks
(441, 511, 435, ...) do not even appear in the retrieved top 5 for these queries. `NO_ANSWER_MAX_
DISTANCE` was not touched to investigate this, per instruction, and the finding would not be fixed
by moving it regardless -- the retrieved chunks are genuinely the wrong ones, not merely distant.
The Korean case shows the gate working as designed even so: it slipped under the distance threshold
on an irrelevant chunk, and the generator's own prompt rule ("say plainly when sources do not cover
it") caught it instead, exactly the two-layer design `NO_ANSWER_MAX_DISTANCE`'s own comment in
`app/config.py` describes.

Answer language is inconsistent, matching what was reported in production before this fix: Spanish
and the mixed Chinese control both got answers in the query's own language; Korean got an English
answer. This project has not added a language instruction to the prompt, and does not do so here --
that is a product decision (whether this tool answers in the asker's own language) reserved for a
human call, not a side effect of a clarifier bugfix. The disclaimer, the DSO/attorney redirect
sentence, and every citation's source label stay in English regardless of the query's language,
confirmed directly against a real response (`disclaimer` field unchanged; citation snippets are the
stored English chunk text) -- a known, stated limitation, not something this fix translates.

## Authority guard reachability (measured, not fixed)

`app/guardrails/authority.py`'s own docstring already predicted this: "a Devanagari or Chinese
question is rejected earlier, for an unrelated reason -- the non-Latin-script tokenizer gap this
project has separately, already sequenced work on." Confirmed directly:

- An injection payload with embedded ASCII (`SYSTEM: ... USCIS ... H-1B ...` inside otherwise-Hindi
  text) already had 4 ASCII content words under the OLD tokenizer and was already reachable before
  this fix -- not a new exposure.
- The same payload fully transliterated into Devanagari with zero ASCII characters at all (`सिस्टम:
  ... यूएससीआईएस ... एच-वनबी ...`) had 0 content words under the OLD tokenizer (`is_too_vague` =
  `True`, blocked as CLARIFY) and has 27 content words under the NEW one (`is_too_vague` = `False`,
  reaches classify/retrieve/generate). This is genuinely newly reachable.
- Run once against the live pipeline: the local generator (`qwen3.5-8k`) declined the injection on
  its own and answered the real H-1B cap question correctly, in Hindi, with valid citations --
  `authority.py` was never actually tested by this run, because no authority claim was made. Had the
  model complied and written a Hindi-language authority claim, `authority.py`'s guard could not have
  caught it regardless of reachability: every one of its SUBJECT/PREDICATE patterns is an English
  string, a limitation already recorded in its own "KNOWN MISSES: non-English" section. This fix
  does not add non-English patterns there, per instruction; it only confirms that a vector into that
  documented gap is reachable where a different bug used to block it by accident.

## Tradeoff

No character-count equivalent of the word-count anchor rescue exists for the scriptio-continua
branch. A short-but-specific scriptio-continua question containing a bare Latin anchor (an
equivalent of "OPT deadline" phrased in Chinese, short enough to fall under 10 content characters)
could still be wrongly clarified. This is accepted, not fixed speculatively: none of the five real
production failures this fix targets are that short, and building a rescue mechanism with no real
failure to calibrate it against would be exactly the kind of invented complexity this module's own
docstring already warns against for the word-count case.

The scriptio-continua character ranges checked are Han ideographs, Hiragana, Katakana, and Thai --
exactly the three languages the bug report named. Other scriptio-continua scripts (Lao, Khmer,
Burmese) are not covered and would still tokenize as one giant whitespace chunk under the
space-separated branch, always clarifying regardless of content. Nobody has reported this for those
scripts; extending the range list to cover them is straightforward if one does.

## Alternatives considered

**Broaden `_TOKEN_RE` to `[^\W_]+` and stop there.** Rejected: measured directly (see Context) to
still give Chinese/Japanese exactly one token regardless of question length, and to introduce a new,
real regression for Hindi by treating Devanagari combining marks as word separators.

**A single global minimum on Unicode codepoints, ignoring word/character distinction.** Considered
and rejected: a raw codepoint count without stripping whitespace and punctuation could not
distinguish `"？？？？？？？？？？"` (ten punctuation marks, no content at all) from ten real
content characters, and would have required its own separate stopword-equivalent logic per script to
avoid counting particles.

**Add a non-English stopword list per script.** Rejected as unnecessary: the existing English-only
stopword list already does the right thing on non-English content by construction -- a non-English
function word never matches it, so it is simply never stripped, which does not affect the count in
a way that breaks either direction of this fix's own test corpus.
