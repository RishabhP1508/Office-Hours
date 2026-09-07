# 0012: Normalize the generator's native citation markup before verification, don't loosen verification

## Context

Phase 8 round 4 swapped the production generator from the local `qwen3.5-8k` model to Ollama Cloud's
`gpt-oss:120b`. After the swap, 5 of the 21 golden rows started coming back `blocked_unverified`
instead of a real answer.

The cause was not the model getting the answer wrong. `gpt-oss:120b` frequently cites in its own
training-format markup, `【1†L2-L5】`, instead of this project's `[1]` bracket convention.
`app/guardrails/citations.py::parse_cited_indices` only ever recognized `[N]`, so an answer that cited
correctly, just in the wrong shape, read as carrying zero citations. `verify_citations` then did exactly
what it is supposed to do: block a `response_type=ANSWER` with no bracketed citation from rendering. The
guardrail was right; the parser just couldn't see what the model had actually written.

Measured over 15 real hosted generations on the 5 affected questions: 9 used `[N]`, 6 used
`【N†...】`, and 0 produced no citation at all. The model always cited something. The project just
couldn't always read it.

## Decision

Normalize the markup before verification runs, and reinforce the prompt. Neither change touches the
verification logic itself.

`app/prompts.py::normalize_native_citation_markup` rewrites every `【N†...】` (or bare `【N】`) span in
the generated answer to `[N]`, and `app/pipeline.py` calls it on the raw answer text before
`verify_citations` ever sees it. It only rewrites the shape of a span that already names a numeric
index: text with no `【...】` markup at all passes through unchanged, and an out-of-range `N` still
gets rejected exactly as an out-of-range `[N]` already was. It cannot manufacture a citation that isn't
there, and it cannot make an ungrounded claim pass. Both system prompts (`SYSTEM_PROMPT`,
`REFUSAL_SYSTEM_PROMPT`) now also say explicitly: use a plain ASCII bracket like `[2]`, never a
full-width bracket citation like `【1†source】`, and never any other citation format.

The verification boundary did not move. An answer with a citation index outside the retrieved range is
still blocked. An `ANSWER`-type response with no citation, native markup or otherwise, is still blocked.
What changed is that the check now sees what the model actually wrote.

## Tradeoff and alternatives considered

**Loosen the citation check to accept `【N†...】` as a valid citation format outright.** Rejected. The
check exists to guarantee every claim maps to a retrieved chunk; widening what counts as "cited" without
first normalizing to one canonical form is how a verifier quietly grows blind spots. Normalizing to the
one format the rest of the codebase already reasons about, then verifying that, keeps there being exactly
one citation convention to get right.

**Accept the blocks as a cost of the model swap.** Rejected. Five real factual questions returned nothing
to the user because of a markup mismatch, not because the answer was ungrounded. A safety guardrail that
blocks a correct, cited answer is not a safety win; it is a false positive with the same shape as a real
one, and the difference matters to whoever asked the question.

**The general lesson.** A citation convention is part of the prompt contract with the generator, not an
incidental detail of parsing. Swapping the model that fills that contract can silently break the
convention in a way that looks, from the eval numbers alone, like a quality regression (more blocked
answers) rather than what it actually was: a format the parser had never been told to expect. Any future
generator swap should check this specific thing before trusting a drop in answer rate as a real finding
about the model.
