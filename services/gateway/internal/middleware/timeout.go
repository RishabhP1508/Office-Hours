// Package middleware holds the gateway's chi middleware: rate limiting, PII redaction, timeouts,
// and tracing.
package middleware

import (
	"context"
	"errors"
	"net/http"
	"time"
)

// ErrTimeToFirstByteExceeded is the cause TimeToFirstByte's context carries when ITS OWN timer is
// what ended it -- as opposed to the parent context ending it for an unrelated reason (the client
// disconnecting, cancelling r.Context()). Both look identical as a plain context.Canceled to
// anything that only calls ctx.Err(); context.Cause(ctx) is what actually tells them apart, and
// internal/proxy/proxy.go's ProxyStream checks it specifically so a real timeout is reported as
// 504 ("the upstream did not respond in time"), not the generic 502 a disconnected client would
// otherwise be indistinguishable from.
var ErrTimeToFirstByteExceeded = errors.New("time to first byte exceeded")

// Timeout wraps a request's context in context.WithTimeout(ctx, d) -- CLAUDE.md's mandate for
// every upstream call, applied here to the WHOLE request/response cycle. Used on /v1/query and
// /v1/sources/status, both of which return a single JSON body, never a stream.
//
// Deliberately NOT used on /v1/query/stream: a real query against the local generator can take
// 20-140 seconds end to end, so bounding the whole call at 15s would break every real streamed
// query. See TimeToFirstByte below for the bound that route actually needs, and
// docs/adr/0007-go-python-split.md for the reasoning in full.
func Timeout(d time.Duration) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx, cancel := context.WithTimeout(r.Context(), d)
			defer cancel()
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// TimeToFirstByte returns a context derived from parent that is canceled if `d` elapses before
// the caller invokes the returned `stop` function. It is NOT a middleware in the wrap-the-whole-
// handler sense: "time to first byte" is an event that happens partway through the SSE proxy
// handler's own work (the moment the upstream response's headers arrive), not at a request/
// response boundary a middleware can see from outside -- so this is a helper the handler itself
// calls, stopping the timer exactly when it has what it needs.
//
// No fixed-duration sleep anywhere: the bound is time.AfterFunc(d, cancel), and `stop` is exactly
// Timer.Stop. Once `stop` has been called, `d` no longer applies -- ctx stays alive until
// `parent` itself ends (e.g. the client disconnecting, which cancels the request's own context and
// therefore this derived one too), which is what lets a real 20-140s stream complete once its
// headers have already arrived within the bound.
func TimeToFirstByte(parent context.Context, d time.Duration) (ctx context.Context, stop func()) {
	ctx, cancel := context.WithCancelCause(parent)
	timer := time.AfterFunc(d, func() { cancel(ErrTimeToFirstByteExceeded) })
	return ctx, func() { timer.Stop() }
}
