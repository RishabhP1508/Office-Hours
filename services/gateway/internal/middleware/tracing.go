package middleware

import (
	"context"

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
func SetupTracing(ctx context.Context, endpoint, serviceName string) (shutdown func(context.Context) error, err error) {
	exporter, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(endpoint))
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
