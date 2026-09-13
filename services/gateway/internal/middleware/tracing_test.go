package middleware

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

// exportOneSpanTo points SetupTracing at a local OTLP receiver, emits one span through the global
// provider SetupTracing installs, flushes, and returns the request paths the receiver actually saw.
//
// The path is the whole point. SetupTracing appends "/v1/traces" to the base endpoint itself rather
// than letting otlptracehttp default it, because WithEndpointURL takes a non-empty path VERBATIM
// (see SetupTracing's own doc comment). When that derivation is wrong the exporter still builds,
// still batches and still reports no error -- the only symptom is spans quietly never arriving in
// Grafana, which no other test in this suite would notice.
func exportOneSpanTo(t *testing.T, endpointPath string) []string {
	t.Helper()

	var mu sync.Mutex
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	ctx := context.Background()
	shutdown, err := SetupTracing(ctx, srv.URL+endpointPath, "tracing-test")
	if err != nil {
		t.Fatalf("SetupTracing returned an error: %v", err)
	}

	_, span := otel.Tracer("tracing-test").Start(ctx, "probe")
	span.End()

	// Shutdown flushes the batcher, so it is the flush barrier here -- not a sleep waiting for the
	// batch interval to elapse.
	flushCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if err := shutdown(flushCtx); err != nil {
		t.Fatalf("flushing spans failed: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	return append([]string(nil), paths...)
}

func assertExportPath(t *testing.T, paths []string, want string) {
	t.Helper()

	if len(paths) == 0 {
		t.Fatalf("the OTLP receiver saw no export request at all; expected a POST to %q", want)
	}
	for _, got := range paths {
		if got != want {
			t.Fatalf("spans were exported to %q, want %q", got, want)
		}
	}
}

// The PRODUCTION shape: an endpoint that already carries a path segment (Grafana Cloud's
// ".../otlp"). This is the case SetupTracing exists to fix -- otlptracehttp's own cleanPath default
// only substitutes "/v1/traces" when the given path is EMPTY, so a non-empty path is left alone and
// spans would post to ".../otlp" with no suffix at all.
func TestSetupTracing_EndpointCarryingAPathSegmentPostsBelowThatSegment(t *testing.T) {
	assertExportPath(t, exportOneSpanTo(t, "/otlp"), "/otlp/v1/traces")
}

// A trailing slash on the configured endpoint must not produce "/otlp//v1/traces".
func TestSetupTracing_TrailingSlashOnTheEndpointDoesNotDoubleTheSeparator(t *testing.T) {
	assertExportPath(t, exportOneSpanTo(t, "/otlp/"), "/otlp/v1/traces")
}

// The DEV shape (docker-compose's "http://otel-lgtm:4318", no path segment at all). This one would
// resolve correctly even without SetupTracing's manual append, via otlptracehttp's empty-path
// default; it is here so a future change that breaks dev while fixing prod still fails.
func TestSetupTracing_EndpointWithNoPathPostsToV1Traces(t *testing.T) {
	assertExportPath(t, exportOneSpanTo(t, ""), "/v1/traces")
}

// The propagator is the half that makes ONE trace span both services: proxy.go's otelhttp-wrapped
// transport reads this global to inject traceparent into every upstream request. Without it the
// gateway still produces perfectly good local spans and every other test here still passes, while
// the orchestrator starts a brand new trace instead of joining this one.
func TestSetupTracing_InstallsTheW3CTraceContextPropagatorSoTraceIDsReachUpstream(t *testing.T) {
	// Start from a propagator that is deliberately NOT TraceContext, so a pass below means
	// SetupTracing installed one rather than inheriting what another test left in the global.
	otel.SetTextMapPropagator(propagation.Baggage{})

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	ctx := context.Background()
	shutdown, err := SetupTracing(ctx, srv.URL, "tracing-test")
	if err != nil {
		t.Fatalf("SetupTracing returned an error: %v", err)
	}
	defer func() {
		flushCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = shutdown(flushCtx)
	}()

	spanCtx, span := otel.Tracer("tracing-test").Start(ctx, "probe")
	defer span.End()

	req, err := http.NewRequest(http.MethodPost, "http://orchestrator.invalid/query", nil)
	if err != nil {
		t.Fatalf("building the carrier request failed: %v", err)
	}
	otel.GetTextMapPropagator().Inject(spanCtx, propagation.HeaderCarrier(req.Header))

	traceparent := req.Header.Get("traceparent")
	if traceparent == "" {
		t.Fatal("no traceparent header was injected: the orchestrator would start a new trace rather than joining this one")
	}
	if traceID := span.SpanContext().TraceID().String(); !strings.Contains(traceparent, traceID) {
		t.Fatalf("traceparent %q does not carry this span's trace ID %q", traceparent, traceID)
	}
}
