package middleware

import (
	"context"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// SetupTracing wires OpenTelemetry to export spans over OTLP/HTTP to `endpoint` -- the same
// grafana/otel-lgtm endpoint the orchestrator already exports to
// (services/orchestrator/app/telemetry.py) -- tagged with `serviceName`
// ("office-hours-gateway", distinct from the orchestrator's own "office-hours-orchestrator").
//
// Sets two GLOBALS: the TracerProvider (so otelhttp.NewHandler, wrapping the router in
// cmd/gateway/main.go, has something to create server spans with) and the TextMapPropagator
// (propagation.TraceContext{}, W3C traceparent). The propagator is what actually makes ONE trace
// span both services: internal/proxy/proxy.go's outbound http.Client is wrapped in
// otelhttp.NewTransport, which reads this global propagator to inject the traceparent header into
// every request it sends upstream -- without it, otelhttp.NewTransport would still create a
// client span locally, but the header carrying that span's IDs would never leave this process,
// and the orchestrator's own span would start a brand new trace instead of joining this one.
//
// Returns a shutdown func the caller must defer (flushes any spans still buffered before the
// process exits).
//
// THE URL RULE, SHARED WITH services/orchestrator/app/telemetry.py (Phase 8 round 4): `endpoint`
// is the BASE OTLP URL only (e.g. "https://otlp-gateway-prod-us-east-0.grafana.net/otlp"), with no
// "/v1/traces" suffix and an optional trailing slash. This function strips any trailing slash and
// appends exactly "/v1/traces" itself, then passes the FULL result to
// otlptracehttp.WithEndpointURL -- deliberately NOT otlptracehttp.WithEndpoint(host), because
// WithEndpointURL takes whatever path the given URL carries VERBATIM as the request path
// (confirmed against otlptracehttp v1.35.0 source, internal/otlpconfig/options.go:280 --
// `cfg.Traces.URLPath = u.Path`), with no automatic "/v1/traces" append the way the OTLP spec's
// general-endpoint convention implies.
//
// CORRECTED (Phase 8 round 5): an earlier version of this comment claimed dev's own
// OTEL_EXPORTER_OTLP_ENDPOINT ("http://otel-lgtm:4318", no path segment at all) was ALSO silently
// broken before this fix, and that the real Phase 7 cross-language trace showed only half a trace.
// That is wrong. otlptracehttp's final config step runs
// `cfg.Traces.URLPath = cleanPath(cfg.Traces.URLPath, DefaultTracesPath)`, and cleanPath
// substitutes the default "/v1/traces" whenever the given path is EMPTY -- dev's endpoint has an
// empty path, so even code that passed it straight to WithEndpointURL with no manual "/v1/traces"
// append (i.e., before this fix existed) would still have resolved to the correct path in dev, by
// that same default. Dev was never actually broken, and the real Phase 7 trace (which ran against
// dev) genuinely did span both services in Tempo. The bug this fix addresses is real but narrower:
// a PRODUCTION-style endpoint whose URL already carries a non-empty path segment (e.g. ".../otlp")
// is left alone by cleanPath rather than defaulted, so THAT endpoint -- untested until this fix --
// would have silently posted to ".../otlp" with no "/v1/traces" suffix, one service (Python, which
// has always appended the suffix itself) working and this one not. Making the append explicit here
// fixes that real, previously-untested production case and leaves dev's already-correct behavior
// unchanged either way. Both services must derive the SAME final URL from the SAME input; this is
// that shared rule, applied on this side.
func SetupTracing(ctx context.Context, endpoint, serviceName string) (shutdown func(context.Context) error, err error) {
	tracesURL := strings.TrimRight(endpoint, "/") + "/v1/traces"
	exporter, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(tracesURL))
	if err != nil {
		return nil, err
	}

	res := resource.NewSchemaless(
		attribute.String("service.name", serviceName),
	)

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
	)

	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.TraceContext{})

	return tp.Shutdown, nil
}
