package middleware

import (
	"context"
	_ "embed"
	"log"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

//go:embed ratelimit.lua
var tokenBucketScript string

// RateLimiter is a Redis-backed token bucket, one bucket per client IP. See ratelimit.lua for the
// refill math and why it all runs in one atomic script rather than a read-modify-write pair of
// round trips.
type RateLimiter struct {
	client   redis.UniversalClient
	capacity int
	refill   float64
	failOpen bool
	script   *redis.Script
	// Now supplies the "current time" passed to ratelimit.lua as an explicit argument (rather
	// than the script reading Redis's own TIME command -- see ratelimit.lua's comment on ARGV[4]
	// for why: verified empirically that miniredis's FastForward, used to advance time in tests
	// without a real sleep, does not affect what TIME returns). Defaults to time.Now; tests
	// substitute a controllable clock instead.
	Now func() time.Time
}

// NewRateLimiter builds a RateLimiter. `failOpen` decides what Allow does when Redis itself is
// unreachable: true means "let the request through" (this gateway's chosen default -- see
// docs/adr/0007-go-python-split.md for why), false means "return 429 as if the bucket were
// empty." Exposed as a constructor argument (not hardcoded) so a test can exercise both policies
// against the same RateLimiter type.
func NewRateLimiter(client redis.UniversalClient, capacity int, refillPerSecond float64, failOpen bool) *RateLimiter {
	return &RateLimiter{
		client:   client,
		capacity: capacity,
		refill:   refillPerSecond,
		failOpen: failOpen,
		script:   redis.NewScript(tokenBucketScript),
		Now:      time.Now,
	}
}

// Allow asks Redis for one token for `key`. `retryAfter` is only meaningful when `allowed` is
// false: how long until at least one token accrues, for the 429 response's Retry-After header.
// `err` is non-nil only when Redis itself could not be reached or returned something this
// function cannot parse; `allowed` in that case reflects the configured fail-open/fail-closed
// policy, so RateLimitMiddleware never has to special-case a Redis outage itself.
func (l *RateLimiter) Allow(ctx context.Context, key string) (allowed bool, retryAfter time.Duration, err error) {
	now := float64(l.Now().UnixNano()) / 1e9
	res, scriptErr := l.script.Run(ctx, l.client, []string{key}, l.capacity, l.refill, 1, now).Result()
	if scriptErr != nil {
		return l.failOpen, fallbackRetryAfter(l.failOpen), scriptErr
	}

	values, ok := res.([]interface{})
	if !ok || len(values) != 2 {
		return l.failOpen, fallbackRetryAfter(l.failOpen), nil
	}

	allowedStr, _ := values[0].(string)
	tokensStr, _ := values[1].(string)
	allowedN, convErr := strconv.Atoi(allowedStr)
	tokensRemaining, tokErr := strconv.ParseFloat(tokensStr, 64)
	if convErr != nil || tokErr != nil {
		return l.failOpen, fallbackRetryAfter(l.failOpen), nil
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

func fallbackRetryAfter(failOpen bool) time.Duration {
	if failOpen {
		return 0
	}
	return time.Second
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
			allowed, retryAfter, err := limiter.Allow(r.Context(), key)
			if err != nil {
				log.Printf("rate limiter: redis error for key %s: %v", key, err)
			}
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
