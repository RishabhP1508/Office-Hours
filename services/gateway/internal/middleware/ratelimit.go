package middleware

import (
	"context"
	_ "embed"
	"fmt"
	"log"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

//go:embed ratelimit.lua
var tokenBucketScript string

// RateLimiter is a Redis-backed token bucket, one bucket per client IP. See ratelimit.lua for the
// refill math and why it all runs in one atomic script rather than a read-modify-write pair of
// round trips.
//
// RED-TEAM FIX (2026-09-07, docs/adr/0016-rate-limiter-fallback-not-fail-open.md): this used to
// take a `failOpen bool` and, on any Redis error, either let every request through (failOpen=true,
// this gateway's actual production default) or deny every request (failOpen=false). failOpen=true
// was the defect: measured against production, 180 concurrent requests against a
// RATE_LIMIT_BUCKET_CAPACITY=20 bucket got zero 429s, because REDIS_URL had silently defaulted to
// an address with no Redis running behind it (see internal/config/config.go) and this type's
// fail-open policy let every one of those 180 requests through with nothing surfacing the
// degradation. failOpen=false is not an acceptable replacement either: it takes the WHOLE gateway
// down on a transient Redis blip, trading "no rate limiting" for "no service at all." Both
// extremes are gone now. Allow ALWAYS limits: when Redis answers, the shared, cross-instance
// Redis bucket governs, exactly as before; when it errors, `fallback` -- an in-process,
// per-gateway-instance token bucket with the SAME capacity and refill -- governs instead, and
// every activation is logged at WARN with the real Redis error, counted (FallbackActivations),
// and recorded as a span attribute, so degradation is never silent again.
type RateLimiter struct {
	client   redis.UniversalClient
	capacity int
	refill   float64
	script   *redis.Script
	// Now supplies the "current time" passed to ratelimit.lua as an explicit argument (rather
	// than the script reading Redis's own TIME command -- see ratelimit.lua's comment on ARGV[4]
	// for why: verified empirically that miniredis's FastForward, used to advance time in tests
	// without a real sleep, does not affect what TIME returns). Defaults to time.Now; tests
	// substitute a controllable clock instead. Also used by `fallback` below, so both the Redis
	// path and the in-process fallback path advance together under an injected test clock.
	Now func() time.Time

	// fallback is the in-process token bucket Allow uses whenever Redis itself cannot be reached
	// or returns something unparseable. See localBucketStore's own doc comment for what it is
	// weaker than and why that is still strictly better than the two alternatives (allow
	// everything, or deny everything).
	fallback *localBucketStore

	// FallbackActivations counts how many Allow calls were governed by `fallback` rather than by
	// Redis -- exported so cmd/gateway/main.go (or a test) can observe degradation directly,
	// without parsing log output. Never reset; a monotonically increasing count of "this instance
	// has had to fall back N times since it started."
	FallbackActivations atomic.Int64
}

// NewRateLimiter builds a RateLimiter backed by `client`, with an in-process fallback bucket of
// the SAME capacity/refill for when `client` cannot be reached (see RateLimiter's own doc comment
// -- there is no more failOpen/failClosed choice to make; Allow always limits, one way or the
// other).
func NewRateLimiter(client redis.UniversalClient, capacity int, refillPerSecond float64) *RateLimiter {
	return &RateLimiter{
		client:   client,
		capacity: capacity,
		refill:   refillPerSecond,
		script:   redis.NewScript(tokenBucketScript),
		Now:      time.Now,
		fallback: newLocalBucketStore(capacity, refillPerSecond),
	}
}

// Allow asks Redis for one token for `key`. `retryAfter` is only meaningful when `allowed` is
// false: how long until at least one token accrues, for the 429 response's Retry-After header.
// `err` is non-nil only when Redis itself could not be reached or returned something this
// function cannot parse; `allowed`/`retryAfter` in that case come from the in-process fallback
// bucket instead (see allowViaFallback), never from an unconditional true/false -- so
// RateLimitMiddleware never has to special-case a Redis outage itself, and a Redis outage can
// never mean "no limiting."
func (l *RateLimiter) Allow(ctx context.Context, key string) (allowed bool, retryAfter time.Duration, err error) {
	now := float64(l.Now().UnixNano()) / 1e9
	res, scriptErr := l.script.Run(ctx, l.client, []string{key}, l.capacity, l.refill, 1, now).Result()
	if scriptErr != nil {
		return l.allowViaFallback(ctx, key, scriptErr)
	}

	values, ok := res.([]interface{})
	if !ok || len(values) != 2 {
		return l.allowViaFallback(ctx, key, fmt.Errorf("unexpected redis script reply shape: %#v", res))
	}

	allowedStr, _ := values[0].(string)
	tokensStr, _ := values[1].(string)
	allowedN, convErr := strconv.Atoi(allowedStr)
	tokensRemaining, tokErr := strconv.ParseFloat(tokensStr, 64)
	if convErr != nil || tokErr != nil {
		return l.allowViaFallback(ctx, key, fmt.Errorf(
			"unparseable redis script reply: allowed=%q tokens=%q", allowedStr, tokensStr))
	}

	if allowedN == 1 {
		return true, 0, nil
	}

	deficit := 1.0 - tokensRemaining
	if deficit < 0 {
		deficit = 0
	}
	seconds := deficit / l.refill
	return false, time.Duration(seconds * float64(time.Second)), nil
}

// allowViaFallback is the ONLY path Allow ever takes when Redis itself could not answer (or
// answered with something this code cannot parse). It makes the degradation impossible to miss:
//   - FallbackActivations increments, so a caller can observe how often this has happened;
//   - a WARN-level log line names the real Redis error (never swallowed, never replaced with a
//     generic message) and the key being governed;
//   - the active span (a no-op if none is present, e.g. in a unit test with a bare
//     context.Background()) gets attributes marking this request as degraded, so a trace makes
//     the same fact visible end to end, the same way CLAUDE.md's phase 8 round flagged
//     app/guardrails/classifier.py's own model-unavailable fallback ought to be visible rather
//     than silent.
//
// This closes the exact pattern CLAUDE.md's red-team round named: "app/guardrails/classifier.py
// does the same thing [silent fail-open]; do not add a third instance of it." This is the second
// instance (config.go's removed default was the first fix); it stays visible, not silent.
func (l *RateLimiter) allowViaFallback(ctx context.Context, key string, redisErr error) (bool, time.Duration, error) {
	l.FallbackActivations.Add(1)

	span := trace.SpanFromContext(ctx)
	span.SetAttributes(
		attribute.Bool("ratelimit.degraded", true),
		attribute.String("ratelimit.mode", "fallback_inprocess"),
		attribute.String("ratelimit.redis_error", redisErr.Error()),
	)

	log.Printf(
		"WARN: rate limiter: Redis unreachable for key %s (%v); falling back to an in-process, "+
			"per-gateway-instance token bucket -- this limits less strongly than the shared Redis "+
			"bucket (per instance, not global across every gateway instance) but NEVER allows "+
			"every request the way the old fail-open behavior did",
		key, redisErr,
	)

	allowed, retryAfter := l.fallback.Allow(key, l.Now())
	return allowed, retryAfter, redisErr
}

// PingRedisWithRetries checks Redis reachability at startup with a bounded number of attempts,
// each bounded by its own timeout -- never a time.Sleep between attempts (CLAUDE.md forbids
// sleeping to retry anywhere in this gateway): a failed attempt's own dial/read timeout already
// consumes real wall-clock time before the next attempt starts, so back-to-back attempts do not
// hammer an unreachable Redis in a tight loop. Returns the LAST error if every attempt fails, nil
// the moment one succeeds. Never blocks gateway startup on failure -- the caller (cmd/gateway/
// main.go) logs the outcome and starts serving either way, degraded to the in-process fallback
// bucket above until Redis recovers.
func PingRedisWithRetries(ctx context.Context, client redis.UniversalClient, attempts int, perAttemptTimeout time.Duration) error {
	var lastErr error
	for i := 0; i < attempts; i++ {
		attemptCtx, cancel := context.WithTimeout(ctx, perAttemptTimeout)
		lastErr = client.Ping(attemptCtx).Err()
		cancel()
		if lastErr == nil {
			return nil
		}
	}
	return lastErr
}

// localBucket is one client key's in-process token-bucket state: the same refill math
// ratelimit.lua implements in Redis, kept in memory instead.
type localBucket struct {
	tokens float64
	lastTS float64 // Unix seconds, fractional
}

// localBucketStore is RateLimiter's fallback when Redis cannot be reached: the SAME token-bucket
// algorithm ratelimit.lua runs in Redis, kept in this process's own memory instead of a shared
// store. It is deliberately weaker than the Redis bucket in exactly one way -- it limits PER
// GATEWAY INSTANCE, not globally across every instance behind a load balancer -- and that is an
// accepted, documented tradeoff, not an oversight: per-instance limiting is strictly better
// protection than the alternative this replaces (no limiting at all, the actual production
// defect), even though it is weaker than the shared Redis bucket it stands in for. A long Redis
// outage grows this store's map by one entry per distinct client key seen during the outage; that
// is an accepted, documented cost of a fallback meant for transient outages, not a general-purpose
// replacement for Redis.
type localBucketStore struct {
	mu       sync.Mutex
	buckets  map[string]*localBucket
	capacity float64
	refill   float64
}

func newLocalBucketStore(capacity int, refillPerSecond float64) *localBucketStore {
	return &localBucketStore{
		buckets:  make(map[string]*localBucket),
		capacity: float64(capacity),
		refill:   refillPerSecond,
	}
}

// Allow mirrors ratelimit.lua's own math exactly (refill by elapsed time since this key's own
// last request, capped at capacity, then spend one token if available), just without Redis.
func (s *localBucketStore) Allow(key string, now time.Time) (allowed bool, retryAfter time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()

	nowSeconds := float64(now.UnixNano()) / 1e9
	b, ok := s.buckets[key]
	if !ok {
		b = &localBucket{tokens: s.capacity, lastTS: nowSeconds}
		s.buckets[key] = b
	}

	elapsed := nowSeconds - b.lastTS
	if elapsed < 0 {
		elapsed = 0
	}
	b.tokens += elapsed * s.refill
	if b.tokens > s.capacity {
		b.tokens = s.capacity
	}
	b.lastTS = nowSeconds

	if b.tokens >= 1 {
		b.tokens -= 1
		return true, 0
	}

	deficit := 1 - b.tokens
	if deficit < 0 {
		deficit = 0
	}
	seconds := deficit / s.refill
	return false, time.Duration(seconds * float64(time.Second))
}

// clientIP is the rate limiter's per-client key: the TCP connection's own remote address, UNLESS
// that peer is a proxy this gateway has been explicitly told to trust (`trustedProxyCIDRs`), in
// which case the first entry of X-Forwarded-For is used instead.
//
// Trusting X-Forwarded-For unconditionally (the earlier, wrong version of this function) is a
// complete rate-limit bypass: a caller sends a different X-Forwarded-For value on every request
// and gets a fresh, empty bucket every time, since nothing about the header is verified against
// who is actually making the TCP connection. Confirmed against a running gateway before this fix:
// a client that would otherwise have been 429'd after 20 requests got 200 on 10 further requests
// simply by rotating a fabricated X-Forwarded-For value, with the real limit never engaging again.
// This is why the peer address is checked FIRST, and the header is read only when that check
// passes -- see internal/config/config.go's TrustedProxyCIDRs (default empty, so this reduces to
// "always key on the peer" for the docker-compose deployment this gateway actually runs as today).
func clientIP(r *http.Request, trustedProxyCIDRs []*net.IPNet) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}

	if isTrustedProxyPeer(host, trustedProxyCIDRs) {
		if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
			first := strings.TrimSpace(strings.Split(xff, ",")[0])
			// The forwarded value must itself parse as an IP before it is trusted as a bucket
			// key -- otherwise a garbage header from a trusted proxy (or a bug in it) could still
			// mint an unbounded number of distinct Redis keys, one per garbage string sent.
			if parsed := net.ParseIP(first); parsed != nil {
				return parsed.String()
			}
		}
	}

	return host
}

func isTrustedProxyPeer(host string, trustedProxyCIDRs []*net.IPNet) bool {
	peerIP := net.ParseIP(host)
	if peerIP == nil {
		return false
	}
	for _, cidr := range trustedProxyCIDRs {
		if cidr.Contains(peerIP) {
			return true
		}
	}
	return false
}

// RateLimitMiddleware returns 429 IMMEDIATELY -- never waits, never sleeps, never queues -- when
// the requesting IP's token bucket is empty, with a Retry-After header (seconds) saying how long
// until at least one token accrues. `trustedProxyCIDRs` is threaded straight through to clientIP;
// see that function and internal/config/config.go's TrustedProxyCIDRs for what it means and why it
// defaults to empty.
func RateLimitMiddleware(limiter *RateLimiter, trustedProxyCIDRs []*net.IPNet) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			key := "ratelimit:" + clientIP(r, trustedProxyCIDRs)
			// err (non-nil exactly when Allow fell back to the in-process bucket) is intentionally
			// not logged again here -- Allow's own allowViaFallback already logged it at WARN,
			// with more context (the real Redis error and which key), the moment it happened.
			allowed, retryAfter, _ := limiter.Allow(r.Context(), key)
			if !allowed {
				w.Header().Set("Retry-After", strconv.Itoa(int(retryAfter.Seconds())+1))
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusTooManyRequests)
				_, _ = w.Write([]byte(`{"error":"rate limit exceeded"}`))
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}
