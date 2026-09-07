"""OpenTelemetry setup.

One TracerProvider, exported over OTLP/HTTP to the otel-lgtm dev backend (or, in production,
Grafana Cloud's OTLP gateway -- see Settings.OTEL_EXPORTER_OTLP_ENDPOINT's own comment), with
FastAPI auto-instrumented so every request gets a root span. The pipeline creates two child spans
per request, named exactly `retrieve` and `generate`, so a single trace shows both steps and how
long each took.

THE URL RULE, SHARED WITH services/gateway/internal/middleware/tracing.go (Phase 8 round 4): both
services build the final traces URL by stripping any trailing slash off
OTEL_EXPORTER_OTLP_ENDPOINT and appending exactly "/v1/traces" (this file also appends "/v1/metrics"
for its own metrics-only path -- the gateway emits no metrics, so it has no equivalent). This
MUST be the same rule in both languages, because Go's otlptracehttp.WithEndpointURL takes the URL
it is given VERBATIM as the request path (confirmed against otlptracehttp v1.35.0 source,
internal/otlpconfig/options.go:280 -- `cfg.Traces.URLPath = u.Path`), unlike Python's
OTLPSpanExporter here, which has always appended the path itself.
CORRECTED (Phase 8 round 5): an earlier version of this comment claimed dev's own
OTEL_EXPORTER_OTLP_ENDPOINT ("http://otel-lgtm:4318", no path at all) was ALSO silently broken by
this before it was made explicit, and that the Phase 7 cross-language trace itself showed only half
a trace. That is wrong, and re-deriving it from otlptracehttp's own source shows why: the SDK's
final config step runs `cfg.Traces.URLPath = cleanPath(cfg.Traces.URLPath, DefaultTracesPath)`, and
cleanPath substitutes the default "/v1/traces" whenever the given path is EMPTY. Dev's endpoint has
an empty path, so even code that passed it straight to otlptracehttp.WithEndpointURL with no manual
"/v1/traces" append (i.e., before this fix existed at all) would still have resolved to the correct
path in dev, by that same default -- dev was never actually broken, and the real Phase 7 trace,
which ran against dev, genuinely did span both services (Tempo showed spans from both
office-hours-gateway and office-hours-orchestrator under one trace ID). The bug this fix addresses
is real but narrower: a PRODUCTION-style endpoint whose URL already carries a real path segment
(e.g. "https://otlp-gateway-prod-us-east-0.grafana.net/otlp", path "/otlp") has a NON-empty path, so
cleanPath leaves it alone rather than defaulting it -- that endpoint would have silently posted to
".../otlp" with no "/v1/traces" suffix at all, one service (Python, which has always appended the
suffix itself) working and the other (Go, before this fix) not. Making the append explicit here
fixes that real, still-untested-in-production case and leaves dev's already-correct behavior
unchanged either way. Set OTEL_EXPORTER_OTLP_ENDPOINT to the BASE OTLP URL only (no "/v1/traces",
trailing slash optional) on both services; do not add a path segment yourself.

Phase 8 round 3 addition: one MeterProvider, exported the same way, backing the observability
dashboard's runtime panels (infra/observability/grafana-dashboard.json). Two counters:
`office_hours_queries_total` (labeled by `response_type` -- backs both the "query volume" panel,
summed across labels, and the "refusal rate" panel, as the ratio of non-"answer" labels to the
total) and `office_hours_generation_calls_total` (originally backed a "cost per day" PROXY panel;
see the Phase 8 round 4 addition below for why that panel was replaced rather than kept). Both are
unconditional, the same way tracing already is: recorded on every request/generation call
regardless of whether Settings.DAILY_GENERATION_CAP (app/usage.py) is configured, since the
dashboard should show real data out of the box even with the cap feature off.

Phase 8 round 4 addition: `office_hours_generation_tokens_total`, labeled by `token_type`
("prompt" or "completion"), backing the dashboard panel that used to show generation-CALL volume
as a cost proxy. Both Grafana Cloud's and Langfuse's free tiers are genuinely free, so there is no
real dollar cost to estimate a proxy for at all -- this project now has REAL per-token counts
available (app/providers/llm.py::LLM.last_usage, populated from Ollama's own prompt_eval_count/
eval_count or an OpenAI-compatible endpoint's own `usage` block), so the dashboard shows those real
counts directly instead of a call-count stand-in for a dollar figure it cannot compute.
record_generation_tokens_metric is a no-op for any field a provider did not report (StubLLM
reports none at all) -- it never estimates or fabricates a count for a field the provider left
absent.
"""

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import Counter, MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings

_tracer: trace.Tracer | None = None
_query_counter: Counter | None = None
_generation_counter: Counter | None = None
_token_counter: Counter | None = None


def setup_telemetry(settings: Settings, app) -> None:
    global _tracer, _query_counter, _generation_counter, _token_counter
    resource = Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME})

    # rstrip: an operator-supplied trailing slash (e.g. "https://host/otlp/") must not produce a
    # double slash before "/v1/traces" -- see this module's own "THE URL RULE" comment above for
    # why this exact transformation has to match services/gateway/internal/middleware/tracing.go's.
    base_endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT.rstrip("/")

    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{base_endpoint}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(settings.OTEL_SERVICE_NAME)
    FastAPIInstrumentor.instrument_app(app)

    metric_exporter = OTLPMetricExporter(endpoint=f"{base_endpoint}/v1/metrics")
    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=15000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    meter = metrics.get_meter(settings.OTEL_SERVICE_NAME)
    _query_counter = meter.create_counter(
        "office_hours_queries_total",
        description="Every /query and /query/stream response actually served, labeled by "
        "response_type.",
    )
    _generation_counter = meter.create_counter(
        "office_hours_generation_calls_total",
        description="Every successful call to the generator.",
    )
    _token_counter = meter.create_counter(
        "office_hours_generation_tokens_total",
        description="Real, provider-reported token counts per successful generator call, labeled "
        "by token_type (prompt|completion) -- never estimated. See app/providers/llm.py::"
        "LLM.last_usage.",
    )


def get_tracer() -> trace.Tracer:
    if _tracer is None:
        return trace.get_tracer(__name__)
    return _tracer


def record_query_metric(response_type: str) -> None:
    """Increment `office_hours_queries_total` for one served response. A no-op before
    setup_telemetry has run (there is nothing to increment yet) -- mirrors get_tracer's own
    fallback shape rather than raising.
    """
    if _query_counter is not None:
        _query_counter.add(1, {"response_type": response_type})


def record_generation_call_metric() -> None:
    """Increment `office_hours_generation_calls_total` for one successful generator call."""
    if _generation_counter is not None:
        _generation_counter.add(1)


def record_generation_tokens_metric(usage: dict | None) -> None:
    """Add `usage["prompt_tokens"]`/`usage["completion_tokens"]` to
    `office_hours_generation_tokens_total`, labeled `token_type="prompt"`/`"completion"`
    respectively. A no-op before setup_telemetry has run, for a `usage=None` provider (StubLLM
    reports none), or for whichever individual field within `usage` is itself `None` -- this never
    adds a fabricated count for a field the provider did not actually report.
    """
    if _token_counter is None or not usage:
        return
    prompt_tokens = usage.get("prompt_tokens")
    if prompt_tokens is not None:
        _token_counter.add(prompt_tokens, {"token_type": "prompt"})
    completion_tokens = usage.get("completion_tokens")
    if completion_tokens is not None:
        _token_counter.add(completion_tokens, {"token_type": "completion"})
