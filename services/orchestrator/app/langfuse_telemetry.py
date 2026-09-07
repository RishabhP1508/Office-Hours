"""Langfuse wiring (Phase 8 round 4): prompt versions, per-answer traces, and REAL token usage.

Off by default: `setup_langfuse` leaves the module-level client at `None` whenever
Settings.LANGFUSE_PUBLIC_KEY or Settings.LANGFUSE_SECRET_KEY is empty (both empty is the default),
so a fresh clone's `docker compose up` and the CI invariant gate are byte-for-byte unaffected --
nothing in this module does anything unless an operator has actually configured a Langfuse
project. `record_generation_trace` is a silent no-op in that case, mirroring the shape
app/telemetry.py's own `record_query_metric`/`record_generation_call_metric` already use for "not
set up yet".

NEVER BREAKS A REQUEST: every call into the langfuse SDK, including client construction, is
wrapped in a broad `except Exception` that logs a warning and moves on. A third-party
observability SDK failing -- a network hiccup reaching Langfuse, an SDK version mismatch, a
malformed response -- must never be the reason a real, already-verified answer fails to render.
This module is called from app/pipeline.py strictly AFTER `verify_citations` has already decided
whether the answer renders; nothing here can affect that decision.

WHAT GETS RECORDED, per successful generator call:
  - `input`: the user prompt handed to the generator (retrieved context plus the question --
    already redacted of PII in transit if this request arrived through the Go gateway; see
    services/gateway/internal/middleware/pii.go). This is the SAME content-privacy posture the rest
    of this project already accepts for observability: OpenTelemetry spans and application logs
    already see this text; Langfuse is a third addition to that same category, not a new one, and
    is entirely opt-in (see "Off by default" above).
  - `output`: the generated answer text.
  - `model`: the model id that actually served the request (app/providers/llm.py::LLM.model_id --
    already reflects FallbackLLM's own choice of primary vs. fallback).
  - `metadata`: `{"prompt_version": ..., "response_type": ..., "is_advice": ...}`. `prompt_version`
    is app/prompts.py::SYSTEM_PROMPT_VERSION or REFUSAL_SYSTEM_PROMPT_VERSION -- a content hash of
    the EXACT prompt text that produced this trace, so a trace can always be tied back to the
    prompt that made it, and the version changes automatically the moment the prompt text does
    (see that module's own comment for why a hash rather than a hand-maintained number).
  - `usage_details`: REAL token counts only, never estimated -- see
    app/providers/llm.py::LLM.last_usage for what each provider actually reports (Ollama's own
    `prompt_eval_count`/`eval_count`, or an OpenAI-compatible endpoint's own `usage` block), or
    nothing at all when the provider reports none (StubLLM, in particular).

COST, STATED HONESTLY: both Grafana Cloud's and Langfuse's free tiers really are free, so this
project records and reports real TOKEN COUNTS, never a fabricated dollar figure -- see
infra/observability/grafana-dashboard.json's own panel description for why the earlier "cost per
day" proxy panel was replaced by a real token/quota panel rather than kept alongside a fictional
one.

DEPENDENCY WEIGHT, CHECKED BEFORE THIS WAS WRITTEN (see docs/reports/phase-8.md for the full
output): langfuse 4.15.1's own PyPI metadata lists exactly httpx, pydantic, backoff, wrapt,
packaging, opentelemetry-api, opentelemetry-sdk, opentelemetry-exporter-otlp-proto-http, and
typing-extensions as its dependencies -- NONE of langchain, langgraph, ragas, or datasets, and the
three opentelemetry packages are ones this project already depends on. The production image's
module-absence check (services/orchestrator/tests/test_freshness.py's dependency-isolation guard
and test_ci_eval_mode.py's serving-path guard) was re-run after adding this dependency; see
docs/reports/phase-8.md for the confirmed image size.

NOT YET VERIFIED AGAINST A REAL LANGFUSE PROJECT: the keys did not exist at the time this was
written (Phase 8 round 4 -- the user is creating the Langfuse account as this is being built). The
SDK call shape below is this project's best-effort reading of langfuse-python's current (v3+,
OTEL-native) client API. The broad try/except above means a call-shape mismatch fails CLOSED
(traces silently do not appear in Langfuse) rather than breaking /query, but it has not been
smoke-tested end to end against a live Langfuse endpoint. Confirm this once real
LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY values are available, and fix forward here if the real SDK
disagrees with what this module assumes.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_client: Any | None = None


def setup_langfuse(settings) -> None:
    """Build the module-level Langfuse client, or leave it `None` (disabled) when either key is
    empty. Called once at startup (app/main.py's lifespan), the same way app/telemetry.py's
    setup_telemetry is -- but unlike that function, failing to set this up must never prevent the
    service from starting at all, since Langfuse is optional observability, not a serving
    dependency.
    """
    global _client
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        _client = None
        return
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST or None,
        )
    except Exception:  # noqa: BLE001 - optional observability must never block startup
        logger.warning(
            "failed to initialize the Langfuse client; Langfuse tracing stays disabled",
            exc_info=True,
        )
        _client = None


def is_enabled() -> bool:
    """True once `setup_langfuse` has built a real client. Exposed mainly for tests."""
    return _client is not None


def _usage_details(usage: dict | None) -> dict | None:
    if not usage:
        return None
    details = {
        "input": usage.get("prompt_tokens"),
        "output": usage.get("completion_tokens"),
    }
    details = {k: v for k, v in details.items() if v is not None}
    return details or None


def record_generation_trace(
    *,
    name: str,
    model_id: str,
    prompt_version: str,
    response_type: str,
    is_advice: bool,
    user_prompt: str,
    answer_text: str,
    usage: dict | None,
) -> None:
    """Record one Langfuse generation for a single successful call to the generator. A silent
    no-op when Langfuse is disabled (`_client is None`) or when anything about the SDK call itself
    raises -- see this module's own docstring, "NEVER BREAKS A REQUEST".
    """
    if _client is None:
        return
    try:
        with _client.start_as_current_generation(
            name=name,
            model=model_id,
            input=user_prompt,
            metadata={
                "prompt_version": prompt_version,
                "response_type": response_type,
                "is_advice": is_advice,
            },
        ) as generation:
            generation.update(output=answer_text, usage_details=_usage_details(usage))
    except Exception:  # noqa: BLE001 - optional observability must never break /query
        logger.warning("Langfuse trace recording failed", exc_info=True)
