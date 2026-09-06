"""Advice-vs-information classifier: does this question ask the system to resolve the asker's own
decision, or predict their own outcome, rather than just state a rule?

Two layers, always tried in this order:

Layer 1 (`rule_based_advice_signal`) is deterministic and runs everywhere, including CI: a
high-precision substring match over generic English advice-seeking phrasings ("should I", "which
is better", ...). It never needs a model, a network call, or a database, so it is the only layer
the CI invariant gate (LLM_PROVIDER=stub) can exercise at all -- see
docs/adr/0002-advice-vs-information-line.md for why the rule layer decides first rather than
deferring to the model.

Layer 2 (the model escalation inside `classify_advice`) runs ONLY when Layer 1 found no signal AND
the configured LLM provider is not the stub. It makes one call to the configured LLM with a prompt
that sees nothing but the question text, asking for strict JSON `{"advice": true|false}`. It may
only escalate an information verdict to advice; it can never overturn a Layer 1 advice verdict
back to information, and a failed/unparseable/timed-out call falls back to "information" rather
than raising -- a classifier outage must never take /query down.

Neither layer is ever handed eval/golden.jsonl's `is_advice` label, directly or indirectly: both
take only the raw question string. Using the label to decide the label would make the
classification a tautology instead of a measurement, the same principle app/providers/llm.py's
StubLLM has always followed for its own (now-removed) advice heuristic.
"""

import json
import logging
import re
from dataclasses import dataclass

from app.config import Settings
from app.providers.llm import LLM

logger = logging.getLogger(__name__)

# Layer 1: deterministic, generic English advice-seeking phrasings. This list started as
# app/providers/llm.py::_STUB_ADVICE_PATTERNS, moved here now that the classifier -- not the stub
# generator -- owns the advice-vs-information decision (see app/pipeline.py). Every pattern here
# was already checked against eval/golden.jsonl's 21 rows and matches either zero or several rows,
# never exactly one: a pattern matching exactly one row would make that row's classification a
# tautology (the pattern was, in effect, reading that row's own label back) rather than a
# measurement. Four patterns used to violate this and were removed before this list was ever moved
# here -- "my odds" and "will uscis count" each matched only the one row they were clearly worded
# after ("What are my odds in the H-1B lottery this year?", "Will USCIS count it as a specialty
# occupation?"), and "should my" and "what should i do" each matched only one row too,
# coincidentally. See services/orchestrator/tests/test_guardrails.py, the test named
# test_advice_patterns_never_match_exactly_one_golden_row, which re-checks this on every run so a
# future edit can't quietly reintroduce a single-row match.
#
# This is a deliberately generic, rough heuristic, not a semantic judgment -- rows this list misses
# (for example "What are my odds..." or "Should my employer...", neither of which contains any
# pattern below) rely entirely on Layer 2 to be caught at all, and Layer 2 is unavailable under the
# stub provider (see classify_advice below). That is an honest, expected gap in CI mode's coverage
# of THIS rule layer alone, not a reason to leave the resulting rate ungated: eval/run.py's CI mode
# gates false_refusal_rate/advice_leakage_rate against a recorded baseline (eval/run.py::
# CI_BASELINE_GATE), same as any other CI metric, precisely because they now come from this real
# production code path rather than a stub-only heuristic. See docs/adr/0004-ci-baselines-vs-
# aspirational-thresholds.md's "Superseded in Phase 4 step 3" section.
ADVICE_PATTERNS = (
    "should i",
    "should we",
    "which is better",
    "which one should",
    "which visa is best",
    "which status is best",
    "is it better to",
    "will i be approved",
    "my chances",
    "what would you do",
    "can you recommend",
    "would you recommend",
)


def rule_based_advice_signal(question: str) -> bool:
    """Layer 1: True if `question` matches a generic advice-seeking phrasing. Sees only the
    question text -- never eval/golden.jsonl's is_advice label."""
    lowered = question.lower()
    return any(pattern in lowered for pattern in ADVICE_PATTERNS)


# Layer 2's own system prompt. Deliberately generic and self-contained: it defines "advice-seeking"
# in its own words rather than depending on any example drawn from eval/golden.jsonl, and it is the
# ONLY thing besides the raw question text that Layer 2 ever sees.
_LAYER2_SYSTEM_PROMPT = """You classify exactly one question as advice-seeking or purely \
informational.

Advice-seeking means the question asks you to resolve the asker's own personal decision, predict \
how their individual case will be decided, or state their personal odds or chances. A request for \
the general rule that applies -- even a long or complex one -- is NOT advice-seeking by itself; \
only requests to apply that rule to the asker's own situation, or to guess the outcome of their \
own case, count as advice-seeking.

Reply with ONLY a compact JSON object and nothing else, exactly one of:
{"advice": true}
{"advice": false}
"""

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_advice_verdict(raw: str) -> bool | None:
    """Extract {"advice": true|false} from a model reply. Returns None (never raises) for anything
    that is not exactly that -- no JSON object found, invalid JSON, or a value that is not a bool
    under the "advice" key.
    """
    match = _JSON_OBJECT_RE.search(raw)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    verdict = data.get("advice")
    if not isinstance(verdict, bool):
        return None
    return verdict


@dataclass(frozen=True)
class ClassificationResult:
    """`decided_by` is one of "rule" (Layer 1 matched), "model" (Layer 2 escalated), or
    "model_unavailable" (Layer 2 was skipped -- LLM_PROVIDER=stub -- or its call failed/timed out/
    returned unparseable output, and the verdict fell back to "information"). This is recorded on
    the classify span's `advice_decided_by` attribute by app/pipeline.py, not returned to the API
    caller, so it is visible in traces without changing the response shape.
    """

    is_advice: bool
    decided_by: str


async def classify_advice(question: str, *, llm: LLM, settings: Settings) -> ClassificationResult:
    """Run Layer 1, then Layer 2 if Layer 1 found nothing and a real LLM provider is configured.

    Layer 2 can only turn a Layer-1 "no signal" into "advice"; it is never consulted at all once
    Layer 1 already said "advice", and it is never consulted when settings.LLM_PROVIDER == "stub"
    (the CI invariant gate's deterministic, network-free provider) -- that gate is what makes
    Layer 2 something a stub-provider test can assert is never called (see
    services/orchestrator/tests/test_guardrails.py,
    test_layer2_not_consulted_when_provider_is_stub).
    """
    if rule_based_advice_signal(question):
        return ClassificationResult(is_advice=True, decided_by="rule")

    if settings.LLM_PROVIDER == "stub":
        return ClassificationResult(is_advice=False, decided_by="model_unavailable")

    try:
        raw = await llm.generate(_LAYER2_SYSTEM_PROMPT, question)
    except Exception:  # noqa: BLE001 - a classifier outage must not take /query down
        logger.warning(
            "Layer 2 advice classifier call failed; falling back to 'information'", exc_info=True
        )
        return ClassificationResult(is_advice=False, decided_by="model_unavailable")

    verdict = _parse_advice_verdict(raw)
    if verdict is None:
        logger.warning(
            "Layer 2 advice classifier returned unparseable output (%r); falling back to "
            "'information'",
            raw[:200],
        )
        return ClassificationResult(is_advice=False, decided_by="model_unavailable")

    return ClassificationResult(is_advice=verdict, decided_by="model")
