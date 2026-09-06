// Package proxy forwards a gateway request to the Python orchestrator: one path for a plain JSON
// request/response (POST /query, GET /sources/status), and one path for the Server-Sent Events
// stream (POST /query/stream) that has to move bytes through unbuffered as they arrive.
package proxy

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	gwmiddleware "office-hours/gateway/internal/middleware"
)

// Proxy holds what every forwarded request needs: where the orchestrator is, and an HTTP client
// whose transport is wrapped so every outbound call carries a W3C traceparent header (see New).
type Proxy struct {
	orchestratorURL string
	client          *http.Client
	streamTimeout   time.Duration
}

// New builds a Proxy. `streamTimeout` is used only by ProxyStream, to bound time-to-first-byte
// (see that method's doc comment); ProxyJSON relies entirely on the context it is handed, which
// internal/middleware/timeout.go's Timeout middleware has already bounded for the routes that use
// it.
//
// The client's Transport is otelhttp.NewTransport wrapping http.DefaultTransport: every outbound
// call gets its own CLIENT span, a child of whatever span is active in the request's context (the
// gateway's own SERVER span, from otelhttp.NewHandler in cmd/gateway/main.go) -- and, critically,
// otelhttp's RoundTripper injects the W3C traceparent header into the outgoing request using the
// GLOBAL TextMapPropagator (internal/middleware/tracing.go sets it), which is what makes the
// orchestrator's own span a CHILD of this one rather than the root of a new trace. The client
// itself carries NO Timeout field: every call's duration is governed by the context it is given,
// never by a client-wide deadline that would not know the difference between "waiting for
// headers" and "streaming a long body."
func New(orchestratorURL string, streamTimeout time.Duration) *Proxy {
	return &Proxy{
		orchestratorURL: orchestratorURL,
		streamTimeout:   streamTimeout,
		client: &http.Client{
			Transport: otelhttp.NewTransport(http.DefaultTransport),
		},
	}
}

// ProxyJSON forwards a non-streaming request to orchestratorURL+upstreamPath, copying the method,
// headers, and (already PII-redacted, by internal/middleware/pii.go, earlier in the chain) body,
// and copying the response straight back. Relies entirely on r.Context() for its bound: the caller
// is expected to have wrapped it in internal/middleware.Timeout already.
func (p *Proxy) ProxyJSON(w http.ResponseWriter, r *http.Request, upstreamPath string) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, "failed to read request body")
		return
	}

	req, err := http.NewRequestWithContext(r.Context(), r.Method, p.orchestratorURL+upstreamPath, bytes.NewReader(body))
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to build upstream request")
		return
	}
	copyRequestHeaders(req, r)

	resp, err := p.client.Do(req)
	if err != nil {
		writeUpstreamError(w, err)
		return
	}
	defer resp.Body.Close()

	copyResponseHeaders(w, resp)
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

// ProxyStream forwards the Server-Sent Events route (POST /query/stream) upstream, unbuffered:
// every chunk read off the upstream response body is written to the client and flushed
// immediately, never accumulated into one buffered write.
//
// THE TIMEOUT SPLIT (see docs/adr/0007-go-python-split.md for the full reasoning): a real query
// against the local generator can take 20-140 seconds end to end, so bounding the WHOLE call at
// 15s the way ProxyJSON's Timeout middleware does would break every real streamed query. What the
// 15s bound actually protects against on THIS route is an upstream that never responds at all --
// hung or unreachable -- not a slow-but-working one. So p.streamTimeout here bounds ONLY the wait
// for the upstream response's HEADERS (internal/middleware.TimeToFirstByte): the instant
// p.client.Do returns (success or failure), the timer is stopped before it can ever fire, and the
// stream that follows is bounded only by the client disconnecting (which cancels r.Context(), and
// therefore the context TimeToFirstByte derived from it) or the upstream closing its body -- never
// by a fixed wall-clock cap. No fixed-duration sleep anywhere in this path.
func (p *Proxy) ProxyStream(w http.ResponseWriter, r *http.Request, upstreamPath string) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeJSONError(w, http.StatusInternalServerError, "streaming unsupported by this response writer")
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, "failed to read request body")
		return
	}

	ctx, stopTimer := gwmiddleware.TimeToFirstByte(r.Context(), p.streamTimeout)
	defer stopTimer()

	req, err := http.NewRequestWithContext(ctx, r.Method, p.orchestratorURL+upstreamPath, bytes.NewReader(body))
	if err != nil {
		stopTimer()
		writeJSONError(w, http.StatusInternalServerError, "failed to build upstream request")
		return
	}
	copyRequestHeaders(req, r)

	resp, err := p.client.Do(req)
	// Headers have either arrived (err == nil) or the call has definitively failed (timeout,
	// connection refused, ...) -- either way, stop the timer before it can fire a cancel that
	// would otherwise race with everything below.
	stopTimer()
	if err != nil {
		// context.WithCancel (which TimeToFirstByte is built on) reports a plain
		// context.Canceled regardless of WHY the context ended -- our own timer firing and the
		// client disconnecting (which cancels r.Context(), and therefore this derived context)
		// look identical to ctx.Err() alone. context.Cause(ctx) is what actually distinguishes
		// them, and it is checked here rather than folded into writeUpstreamError, which knows
		// nothing about this route's own context.
		if errors.Is(context.Cause(ctx), gwmiddleware.ErrTimeToFirstByteExceeded) {
			writeJSONError(w, http.StatusGatewayTimeout, "upstream did not respond in time")
			return
		}
		writeUpstreamError(w, err)
		return
	}
	defer resp.Body.Close()

	copyResponseHeaders(w, resp)
	w.WriteHeader(resp.StatusCode)
	flusher.Flush()

	buf := make([]byte, 4096)
	for {
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			if _, writeErr := w.Write(buf[:n]); writeErr != nil {
				return
			}
			flusher.Flush()
		}
		if readErr != nil {
			return
		}
	}
}

func copyRequestHeaders(dst *http.Request, src *http.Request) {
	for key, values := range src.Header {
		if strings.EqualFold(key, "Content-Length") || strings.EqualFold(key, "Host") {
			continue
		}
		for _, v := range values {
			dst.Header.Add(key, v)
		}
	}
}

// hopByHopHeaders are the headers RFC 7230 section 6.1 says describe ONE connection (client <->
// this gateway, or this gateway <-> the upstream) and must never be forwarded verbatim between
// hops. Transfer-Encoding is the sharpest case: Go's net/http server decides this response's own
// framing itself (chunked or not), and copying the upstream's Transfer-Encoding value into it
// would assert a framing this response does not actually use on the wire -- a latent bug
// independent of the CORS one below, closed by the same skip list.
var hopByHopHeaders = []string{
	"Connection",
	"Keep-Alive",
	"Proxy-Authenticate",
	"Proxy-Authorization",
	"TE",
	"Trailer",
	"Transfer-Encoding",
	"Upgrade",
}

// shouldSkipResponseHeader is true for every header the gateway must decide for ITSELF rather
// than echo from the upstream:
//
//   - every Access-Control-* header, and the Vary header a CORS middleware sets alongside them.
//     go-chi/cors (cmd/gateway/main.go) already answered the browser's CORS preflight/request for
//     THIS response before ProxyJSON/ProxyStream ever ran; the orchestrator's OWN CORS middleware
//     (services/orchestrator/app/main.py) is deliberately left running so a direct curl/script
//     call still works, but its answer belongs to a DIFFERENT response (the one the orchestrator
//     itself would have sent a browser directly) and copying it onto this one duplicates every
//     value: a browser sees `Access-Control-Allow-Origin: http://localhost:3000,
//     http://localhost:3000` and rejects the response outright, not merely warns about it. This
//     was caught in real-browser testing (curl never enforces CORS, so no curl-based check here
//     had ever exercised it).
//   - every hop-by-hop header (see hopByHopHeaders above).
func shouldSkipResponseHeader(key string) bool {
	if strings.HasPrefix(strings.ToLower(key), "access-control-") {
		return true
	}
	if strings.EqualFold(key, "Vary") {
		return true
	}
	for _, h := range hopByHopHeaders {
		if strings.EqualFold(key, h) {
			return true
		}
	}
	return false
}

func copyResponseHeaders(w http.ResponseWriter, resp *http.Response) {
	for key, values := range resp.Header {
		if shouldSkipResponseHeader(key) {
			continue
		}
		for _, v := range values {
			w.Header().Add(key, v)
		}
	}
}

func writeJSONError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(`{"error":"` + message + `"}`))
}

// writeUpstreamError distinguishes a timeout (context deadline exceeded -- reported as 504, "the
// upstream took too long") from any other failure (reported as 502, "the upstream call failed
// outright": connection refused, DNS failure, ...). errors.Is unwraps through *url.Error (which
// http.Client.Do returns failures wrapped in), so this correctly detects context.DeadlineExceeded
// even though it never appears as the outermost error type.
func writeUpstreamError(w http.ResponseWriter, err error) {
	if errors.Is(err, context.DeadlineExceeded) {
		writeJSONError(w, http.StatusGatewayTimeout, "upstream did not respond in time")
		return
	}
	writeJSONError(w, http.StatusBadGateway, "upstream request failed")
}
