"""Programmatic citation verification: does a generated answer only cite context that was actually
retrieved, and does an ANSWER-type response cite anything at all?

Two mechanical checks, neither one a model judgment (see ARCHITECTURE.md, "Every factual claim maps
to a retrieved chunk, checked programmatically"):

1. Every bracketed index in the answer text (`[2]`, or a comma-separated group like `[1, 3]`) must
   fall within 1..len(contexts). An index outside that range cites something that was never
   retrieved for this query -- a hallucinated citation.
2. A response the pipeline is about to render as `ResponseType.ANSWER` must carry at least one
   bracketed citation. A confident factual answer with no citation at all is exactly the "uncited
   claim rendered" failure CLAUDE.md forbids, so it is treated the same as an out-of-range index.

Be honest about what this does NOT do: it verifies that every citation resolves to a chunk that
was retrieved, and that an ANSWER carries at least one citation. It does not audit, sentence by
sentence, whether the specific claim next to a citation is actually supported by the text of the
chunk it cites -- that would need a model to read and compare meaning, and CLAUDE.md's stated
preference is to make a check programmatic wherever a programmatic check is possible rather than
defer to a model's opinion by default. A generated answer can pass both checks here and still
misdescribe what a cited chunk says; that gap is a real limitation of this module, not a hidden
one.

On failure, the generated answer text must not be rendered at all -- the caller (app/pipeline.py)
never puts it in the response, and returns a safe, generic message plus `response_type=
BLOCKED_UNVERIFIED` and a `refusal_reason` naming which check failed.
"""

import re
from dataclasses import dataclass

from app.schemas import ResponseType

_BRACKET_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def parse_cited_indices(answer_text: str) -> set[int]:
    """Every bracketed numeric reference in the answer, including comma-separated groups like
    "[1, 3]". Matches app/prompts.py::format_context's [1]..[k] numbering (the same convention
    eval/run.py's own parse_cited_indices uses against the rendered answer, kept independent here
    since app/ has no dependency on eval/ in production).
    """
    indices: set[int] = set()
    for match in _BRACKET_RE.finditer(answer_text):
        for part in match.group(1).split(","):
            indices.add(int(part.strip()))
    return indices


@dataclass(frozen=True)
class VerificationResult:
    """`reason` is short and machine-readable (it is what app/pipeline.py puts straight into the
    response's `refusal_reason`); `detail` is a longer, human-readable explanation for logs only,
    never rendered to the caller. Both are `None` when `ok=True`.
    """

    ok: bool
    reason: str | None
    detail: str | None = None


def verify_citations(
    answer_text: str, *, num_contexts: int, response_type: str
) -> VerificationResult:
    """Check 1 (out-of-range index) applies to any generated response, ANSWER or REFUSAL_ADVICE
    alike, since both are generated from retrieved context and either could hallucinate an index.
    Check 2 (must cite something) applies only when `response_type == ResponseType.ANSWER` -- see
    the module docstring for why REFUSAL_ADVICE is not held to the same citation-count requirement.
    """
    cited = parse_cited_indices(answer_text)
    valid_range = set(range(1, num_contexts + 1))
    out_of_range = cited - valid_range
    if out_of_range:
        return VerificationResult(
            ok=False,
            reason="citation_index_out_of_range",
            detail=(
                f"cited {sorted(out_of_range)}, but only 1..{num_contexts} context(s) were "
                "retrieved for this question"
            ),
        )
    if response_type == ResponseType.ANSWER and not cited:
        return VerificationResult(
            ok=False,
            reason="answer_missing_citation",
            detail="response_type=ANSWER but the generated answer carries no bracketed citation",
        )
    return VerificationResult(ok=True, reason=None)
