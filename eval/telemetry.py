"""Emits real eval-run metrics to the SAME OTLP endpoint the orchestrator service already exports
traces to (Settings.OTEL_EXPORTER_OTLP_ENDPOINT/OTEL_SERVICE_NAME), so the observability
dashboard's "faithfulness trend" panel (infra/observability/grafana-dashboard.json) is built from
real accumulated eval runs over time -- one gauge data point per run -- rather than a fabricated
series.

Faithfulness is an EVAL-TIME metric, not a request-time one (see ARCHITECTURE.md and eval/run.py's
own module docstring): there is nothing running at request time that could measure it, since it
needs the judge and RAGAS. This module is the chosen answer to that constraint: emit it FROM the
eval run itself, immediately after eval/run.py's main() has already computed aggregate_stats, as a
real metric/gauge pushed to the same backend the runtime dashboard already reads from. (The
alternative CLAUDE.md offered -- reading eval/results/*.json from Grafana directly -- was not
picked: the otel-lgtm image bundles no JSON-file datasource plugin, and installing one would be a
new moving part outside "OTel, exported to grafana/otel-lgtm" per ARCHITECTURE.md's own
constraint.)

This is a REPORT-ONLY side channel. It runs strictly AFTER eval/run.py's main() has already decided
PASS/FAIL from THRESHOLDS/the baseline comparison, reads only the already-computed aggregate_stats
dict, and writes nothing back into it, into eval/golden.jsonl, into THRESHOLDS, or into any judge/
RAGAS call. A failure here (the OTLP endpoint being unreachable, in particular) is caught and
logged, never allowed to change main()'s exit code.

CI mode never calls this at all for anything but a no-op (see emit_run_metrics's own early return):
CI mode computes no faithfulness/comprehensibility/RAGAS metric (see eval/run.py's module
docstring), so there is nothing real to emit there, and emitting a placeholder would be exactly the
"fabricated series" this module exists to avoid.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Which aggregate_stats fields become which gauge, and under what metric name. Every one of these
# is REPORTED-only in eval/run.py itself (see THRESHOLDS) -- nothing about which fields get
# emitted here changes what eval/run.py gates on.
_GAUGE_SPECS = (
    ("office_hours_eval_faithfulness", "faithfulness"),
    ("office_hours_eval_answer_relevancy", "answer_relevancy"),
    ("office_hours_eval_context_precision", "context_precision"),
    ("office_hours_eval_comprehensibility", "comprehensibility"),
    ("office_hours_eval_false_refusal_rate", "false_refusal_rate"),
    ("office_hours_eval_advice_leakage_rate", "advice_leakage_rate"),
)


def emit_run_metrics(
    *,
    otlp_endpoint: str,
    service_name: str,
    ci_mode: bool,
    mode_label: str,
    aggregate_stats: dict,
) -> None:
    """Push one gauge data point per metric in _GAUGE_SPECS, labeled `mode` ("ci" or "full") --
    only in full mode, and only for a field whose value is not None (a metric CI mode always skips,
    or one that errored out for every row in this particular run, is never coerced into a fake
    zero).

    Never raises: any failure (OTLP endpoint unreachable, most commonly, when this runs against a
    dev/CI environment with no otel-lgtm listening) is logged and swallowed, since a dashboard
    metric failing to export must never fail the eval run itself.
    """
    if ci_mode:
        return  # nothing real to emit -- see module docstring

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource

        exporter = OTLPMetricExporter(endpoint=f"{otlp_endpoint}/v1/metrics")
        # export_interval_millis is irrelevant here -- this process calls force_flush() itself,
        # immediately below, rather than waiting on the reader's own background timer, since
        # eval/run.py exits right after this function returns.
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60000)
        resource = Resource.create({SERVICE_NAME: service_name})
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = provider.get_meter("eval.run")

        attributes = {"mode": mode_label}
        emitted = []
        for metric_name, field in _GAUGE_SPECS:
            value = aggregate_stats.get(field, {}).get("value")
            if value is None:
                continue
            gauge = meter.create_gauge(metric_name)
            gauge.set(value, attributes)
            emitted.append(metric_name)

        # This process exits immediately after main() returns; force a synchronous export now
        # rather than relying on the PeriodicExportingMetricReader's own background timer, which
        # would never get a chance to fire.
        provider.force_flush(timeout_millis=10000)
        provider.shutdown(timeout_millis=10000)
        print(f"\nEmitted {len(emitted)} eval-run metric(s) to {otlp_endpoint}: {emitted}")
    except Exception:  # noqa: BLE001 - a dashboard metric failing to export must never fail the run
        logger.warning(
            "Could not emit eval-run metrics to OTLP endpoint %r", otlp_endpoint, exc_info=True
        )
