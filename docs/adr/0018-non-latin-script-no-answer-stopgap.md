# 0018. Non-Latin-script no-answer stopgap (deliberate, documented, temporary)

## Context

Measured against production (`gpt-oss:120b`) on 2026-09-08:

```
옵티 연장은 몇 개월인가요?   ("How many months is the OPT extension?")
-> response_type: answer, 5 citations
-> "The OPT (Optional Practical Training) extension can be granted for up to 12 months [4]."
-> sources: H-1B Electronic Registration FAQ / What if my M-2 visa expired? /
   transition provisions for English language training / OPT for F-1 Students / H-1B Cap Season
```

The question asks about the STEM OPT extension, which is 24 months. The answer says 12 months
(post-completion OPT's own length, a real number but the wrong one for this question), lifted from
one loosely related chunk in an otherwise irrelevant retrieved set. It is a confident, cited, wrong
number delivered to someone who cannot easily verify the English sources it points at.

Root cause, already diagnosed and NOT fixed here: cross-lingual retrieval does not work on this
corpus/embedder. The nearest chunk for that query sits at cosine distance 0.4591 against
`NO_ANSWER_MAX_DISTANCE=0.50`, so the no-answer gate does not fire. No threshold separates it from
answerable English questions: golden row 16 is a legitimate question at 0.4720, ABOVE the Korean
failure's own distance. The real fix is a multilingual embedder, a full re-embed, and re-deriving
`NO_ANSWER_MAX_DISTANCE` from scratch -- out of scope here, and `NO_ANSWER_MAX_DISTANCE` itself was
not touched to build this.

Until that lands: a non-Latin-script question is told honestly that the tool cannot read it yet,
rather than given a wrong answer.

## Decision

`app/pipeline.py::_is_predominantly_non_latin`, called directly after the clarifier's own vague
check (Step 1.5, before classification, before any embedding call): a question is gated to
`ResponseType.NO_ANSWER` / `refusal_reason="non_latin_script_unsupported"` when it contains at
least one non-Latin-script letter (a coarse Unicode-name check: `unicodedata.name(ch, "")` does not
start with `"LATIN"`) AND has zero Latin content words anywhere in it. Content words are
`app/guardrails/clarifier.py`'s own whitespace-tokenized, stopword-stripped extraction (exposed as
a new public `content_words` function, byte-identical to the existing private `_content_words` --
this is a visibility change only, not a new rule), reused rather than writing a second tokenizer.
`refusal_reason="non_latin_script_unsupported"` reuses the existing `NO_ANSWER` response type
rather than adding a sixth member to `ResponseType` -- the same choice, and the same reason, as the
daily-generation-cap degradation already in `app/pipeline.py`: a new enum member would require
`eval/run.py`'s closed `response_type -> refusal_reason` mapping to learn about it too, and that
file is out of scope for this change.

A single bare Latin content word (an anchor like "STEM" or "OPT") is enough to skip the gate
entirely and let the question through to ordinary retrieval. This was measured, not assumed, on the
one genuinely ambiguous case found while building this: a Cyrillic question that keeps "STEM OPT"
as a literal Latin substring.

```
question                                          min distance   retrieved chunk ids (relevant?)
Сколько месяцев длится продление STEM OPT?        0.3461         441,444,435,437,511  all STEM OPT Extension sections
STEM OPT 延期可以延长多少个月？ (Chinese control)    0.3313         441,444,437,511,442  all STEM OPT Extension sections
¿Cuántos meses dura la extensión STEM OPT?         0.3385         441,511,435,444,437  all STEM OPT Extension sections
```

All three land on the identical relevant chunk set, at a distance comfortably inside the in-domain
control range (0.1563-0.3814) that calibrates `NO_ANSWER_MAX_DISTANCE`. Confirmed end to end
against the real local pipeline (real `nomic-embed-text` retrieval, real `qwen3.5-8k` generation),
all three produce a correct, cited "24 months" answer. The mechanism is the RRF keyword arm: a
literal English token like "STEM"/"OPT" is tokenized and matched by Postgres's `tsvector` search the
same way regardless of what script surrounds it, so it is not surprising that Cyrillic behaves like
Chinese and Spanish here rather than like Korean. The Cyrillic-plus-anchor case is therefore treated
as a MUST-NOT-GATE mixed question, not a MUST-GATE pure-non-Latin one, on this evidence -- one bare
anchor is enough, and is not special-cased per script.

## What this trades

**Kept open (deliberately):** any mixed-script question that carries a bare Latin anchor -- proven
working for Chinese, Spanish, and Cyrillic above, and expected to generalize to any other script
paired with the same literal English term, since the mechanism (`tsvector` keyword matching) does
not look at surrounding script at all.

**Closed:** every question that is non-Latin script with no Latin anchor at all, regardless of how
specific or well-formed it is. Measured directly against the running pipeline:

```
옵티 연장은 몇 개월인가요?                                    -> no_answer / non_latin_script_unsupported
我的实习工作许可可以延长多少个月？                              -> no_answer / non_latin_script_unsupported
एसटीईएम ओपीटी एक्सटेंशन कितने महीने का होता है?                -> no_answer / non_latin_script_unsupported
كم عدد الأشهر التي يستغرقها تمديد التدريب العملي؟             -> no_answer / non_latin_script_unsupported
私の就労許可は何ヶ月延長できますか？                             -> no_answer / non_latin_script_unsupported
ใบอนุญาตทำงานของฉันขยายได้กี่เดือน                           -> no_answer / non_latin_script_unsupported
help / opt? / i have a question / visa                       -> clarify / query_too_vague (unaffected -- clarify runs first)
```

This is a real cost, not a free lunch: `我的实习工作许可可以延长多少个月？` is a genuine, specific,
answerable question (ADR 0017's own fix deliberately lets it PAST the clarifier, because it is not
vague), and this gate still declines it, because being specific is not the same thing as being in a
script this corpus/embedder can retrieve against reliably. The judgment made here is that a wrong,
confidently cited number is a worse failure than an honest decline, for a reader who cannot check
the English sources being cited.

## Measured, not assumed: this stopgap does not cover Latin-script languages, and at least one of
## them reproduces the same failure

The gate keys on script alone (by instruction -- extending it to Latin-script languages was
explicitly left to the user's own judgment, not done here). Spanish and French were measured
directly against production, 4 questions each, at least 3 of the 4 per language phrased with no
Latin/English loanword to anchor retrieval on:

| question (language, has loanword?) | response_type | retrieved chunks relevant? | stated number | correct? |
| --- | --- | --- | --- | --- |
| "¿Cuántos meses de prórroga tienen los estudiantes que trabajan en ciencia y tecnología...?" (es, no) | answer | 1 of 5 (chunk 435, STEM OPT Extension) | 24 months | **correct** -- generator found the one relevant chunk in a noisy top-5 |
| "¿Qué es el periodo de gracia después de terminar mi entrenamiento práctico opcional?" (es, no) | answer | 0 of 5 (expired-passport and final-rule-transition chunks; the real "60-day grace period" chunk, id 555, was not retrieved) | "30 days," stated as the current, flat answer | **wrong** -- 30 days is the POST-Sept-15-2026 figure (misread out of chunk 674's AUD table), not today's still-current 60-day grace period; the answer never states both, unlike the French case below |
| "¿Cuánto tiempo tengo que esperar para cambiar de empleador...?" (es, no) | refusal_advice | n/a | none stated | correctly declined as advice-seeking, no number fabricated |
| "¿Cuántos meses dura la extensión STEM OPT?" (es, yes -- control) | answer | 5 of 5 | 24 months | correct |
| "Combien de mois dure la prolongation pour les étudiants en sciences et technologies...?" (fr, no) | answer | 2 of 5 (chunks 435, 511, both STEM OPT Extension) | 24 months | correct |
| "Qu'est-ce que la période de grâce après la fin de ma formation pratique facultative ?" (fr, no) | answer | 2 of 5 (chunk 702 "new departure period," chunk 662 "final rule transition") | "currently 60 days... reduced to 30 days beginning September 15, 2026," both stated with the date | **correct**, and correctly dual-dated per ARCHITECTURE.md |
| "Combien de temps dois-je attendre pour changer d'employeur...?" (fr, no) | refusal_advice | n/a | none stated | correctly declined as advice-seeking |
| "Combien de mois dure l'extension STEM OPT ?" (fr, yes -- control) | answer | 5 of 5 | 24 months | correct |

**The direct answer to "does Spanish or French produce a confident wrong number over an irrelevant
chunk set, the way Korean does": yes, Spanish does, on the measured grace-period question.** The
same corpus contains the correct chunk (id 555, "Can I reenter during the 60-day period after
finishing my program or OPT?"), but it did not make the Spanish query's own top 5, and the generator
filled the gap with a real number pulled from an adjacent but wrong-dated chunk, stated with no
hedge and no mention of the still-current 60-day figure -- structurally the same failure class as
the Korean incident this stopgap exists to close, just in a Latin script the gate does not (and, per
instruction, was not extended to) touch.

French did not reproduce this failure on the 4 questions measured here -- its equivalent
grace-period phrasing happened to retrieve the actual "new departure period" FAQ entry and gave the
fully correct, dual-dated answer. This is NOT evidence that French is safe in general: both
languages share the identical retrieval mechanism, and the French result differing from the Spanish
one on this one question looks like which chunk happened to rank in the top 5 for this specific
phrasing, not a systematic difference between the two languages. Four questions per language is not
an exhaustive test. This finding is reported for the user's own judgment on whether something beyond
script needs to gate on this, per instruction -- the gate in this change was deliberately NOT
extended to Latin-script languages.

## Tradeoff

This is a coarse, mechanical, and known-imperfect rule, not a language-identification model:

- It cannot tell "this pure non-Latin question would have retrieved correctly anyway" from "this one
  would not" -- it declines every pure non-Latin question with no Latin anchor uniformly, some
  fraction of which might have been fine. Untested and unclaimed either way.
- It does nothing for Latin-script languages, and the Spanish measurement above shows at least one
  of them can reproduce the exact failure class this stopgap exists to close.
- The Latin-anchor rescue is binary (one anchor word is enough) rather than proportional to how much
  of the question is anchored. This was a deliberate choice, not an oversight: the one ambiguous case
  measured (Cyrillic, mostly non-Latin words plus one two-word Latin anchor) already retrieved
  correctly, so there was no measured case to justify a stricter (e.g., "at least half the words must
  be Latin") rule instead, and inventing one without a real failure to calibrate it against would be
  exactly the kind of speculative complexity CLAUDE.md warns against.

## Remove this when

Cross-lingual retrieval is good enough that `NO_ANSWER_MAX_DISTANCE` (or whatever threshold replaces
it) separates a real non-Latin question from an unanswerable one on its own, the way it already does
for English. At that point, in one commit: the STOPGAP comment block and the three helper functions
in `app/pipeline.py` (`_is_latin_letter`, `_has_non_latin_letter`, `_is_predominantly_non_latin`),
the Step 1.5 call site in `answer_question`, the `"non_latin_script_unsupported"` refusal reason,
the `content_words` public alias in `app/guardrails/clarifier.py` (unless something else starts using
it by then), and the `_NO_ANSWER_COPY_BY_REASON["non_latin_script_unsupported"]` entry in
`services/frontend/components/Message.tsx` should all come out together. Nothing downstream depends
on any of this once distance alone can do the job.

## Alternatives considered

**Lower `NO_ANSWER_MAX_DISTANCE` further to also catch the Korean incident (0.4591).** Rejected,
and out of scope by instruction: golden row 16 (a real, legitimate English question) sits at 0.4720,
above the Korean failure's own distance -- there is no single threshold value that keeps row 16
answerable and also gates the Korean incident. This is exactly why a script-based signal was needed
instead of a distance-based one.

**A real language-identification library instead of a Unicode-name check.** Rejected for a stopgap:
heavier dependency, slower, and answers a question ("what language is this") this check does not
actually need -- it only needs "does this contain a non-Latin letter," which a Unicode property
lookup answers exactly and immediately, with no model and no extra dependency.

**Require more than one Latin content word before rescuing a non-Latin question.** Considered given
how close the Cyrillic case was to being genuinely ambiguous, and rejected: the measurement showed a
single two-word anchor ("STEM OPT") already retrieves the fully correct chunk set at a comfortable
distance margin in all three scripts tested, so a stricter rule would have gated a question that
demonstrably works, with no real failure observed to justify the stricter threshold instead.

**Extend the gate to Spanish/French given the measured grace-period failure.** Not done, per
instruction: this change's scope was script-based detection only, and the Spanish/French finding is
reported above for the user's own decision, not acted on unilaterally here.
