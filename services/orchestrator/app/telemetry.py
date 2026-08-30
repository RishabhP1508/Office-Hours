"""OpenTelemetry setup.

One TracerProvider, exported over OTLP/HTTP to the otel-lgtm dev backend, with FastAPI
auto-instrumented so every request gets a root span. The pipeline creates two child spans per
request, named exactly `retrieve` and `generate`, so a single trace shows both steps and how long
each took.
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings

_tracer: trace.Tracer | None = None


def setup_telemetry(settings: Settings, app) -> None:
    global _tracer
    resource = Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{settings.OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(settings.OTEL_SERVICE_NAME)
    FastAPIInstrumentor.instrument_app(app)


def get_tracer() -> trace.Tracer:
    if _tracer is None:
        return trace.get_tracer(__name__)
    return _tracer
