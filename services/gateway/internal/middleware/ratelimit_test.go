package middleware

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func newTestRedis(t *testing.T) (*miniredis.Miniredis, *redis.Client) {
	t.Helper()
	mr := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	return mr, client
}

func TestRateLimiter_AllowsRequestsUpToCapacity(t *testing.T) {
	_, client := newTestRedis(t)
	limiter := NewRateLimiter(client, 3, 1.0, false)

	for i := 0; i < 3; i++ {
		allowed, _, err := limiter.Allow(context.Background(), "test-key")
		if err != nil {
			t.Fatalf("request %d: unexpected error: %v", i, err)
		}
		if !allowed {
			t.Fatalf("request %d: expected allowed, bucket had capacity 3", i)
		}
	}
}

func TestRateLimiter_DeniesOnceCapacityIsExhausted(t *testing.T) {
	_, client := newTestRedis(t)
	limiter := NewRateLimiter(client, 2, 1.0, false)

	for i := 0; i < 2; i++ {
		allowed, _, err := limiter.Allow(context.Background(), "exhaust-key")
		if err != nil || !allowed {
			t.Fatalf("request %d: expected allowed, got allowed=%v err=%v", i, allowed, err)
		}
	}

	allowed, retryAfter, err := limiter.Allow(context.Background(), "exhaust-key")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if allowed {
		t.Fatal("expected the 3rd request against a capacity-2 bucket to be denied")
	}
	if retryAfter <= 0 {
		t.Fatalf("expected a positive Retry-After when denied, got %v", retryAfter)
	}
}

// Buckets are keyed independently: exhausting one client's bucket must never affect another
// client's.
func TestRateLimiter_BucketsAreIndependentPerKey(t *testing.T) {
	_, client := newTestRedis(t)
	limiter := NewRateLimiter(client, 1, 1.0, false)

	allowedA, _, err := limiter.Allow(context.Background(), "client-a")
	if err != nil || !allowedA {
		t.Fatalf("client-a's first request should be allowed, got allowed=%v err=%v", allowedA, err)
	}
	deniedA, _, err := limiter.Allow(context.Background(), "client-a")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if deniedA {
		t.Fatal("client-a's second request should be denied (capacity 1)")
	}

	allowedB, _, err := limiter.Allow(context.Background(), "client-b")
	if err != nil || !allowedB {
		t.Fatalf("client-b's first request should be allowed regardless of client-a's state, got allowed=%v err=%v", allowedB, err)
	}
}

// fakeClock lets a test move "now" forward by exact, chosen amounts with no real sleep at all --
// RateLimiter.Now is exactly the seam that makes this possible (see its own doc comment for why
// this exists instead of relying on miniredis's FastForward, which was tried first and verified
// NOT to affect the Lua TIME command miniredis also implements).
type fakeClock struct{ now time.Time }

func (c *fakeClock) Now() time.Time { return c.now }
func (c *fakeClock) Advance(d time.Duration) {
	c.now = c.now.Add(d)
}

// The refill math is driven entirely by RateLimiter.Now, an injected clock -- never a real sleep,
// and never miniredis's FastForward, which does not affect it (see ratelimit.lua's own comment on
// ARGV[4] for the empirical finding that motivated this design).
func TestRateLimiter_RefillsOverTimeUsingAnInjectedClockNotRealSleep(t *testing.T) {
	_, client := newTestRedis(t)
	// capacity 1, refill 1 token/second: after exhausting the single token, advancing the fake
	// clock by 2 seconds should refill enough for exactly one more request.
	limiter := NewRateLimiter(client, 1, 1.0, false)
	clock := &fakeClock{now: time.Now()}
	limiter.Now = clock.Now

	allowed, _, err := limiter.Allow(context.Background(), "refill-key")
	if err != nil || !allowed {
		t.Fatalf("first request should be allowed, got allowed=%v err=%v", allowed, err)
	}

	deniedImmediately, _, err := limiter.Allow(context.Background(), "refill-key")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if deniedImmediately {
		t.Fatal("second request should be denied immediately, before any time passes")
	}

	clock.Advance(2 * time.Second)

	allowedAfterRefill, _, err := limiter.Allow(context.Background(), "refill-key")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !allowedAfterRefill {
		t.Fatal("expected the bucket to have refilled after advancing the clock 2s at 1 token/second")
	}
}

func TestRateLimiter_NeverExceedsCapacityNoMatterHowMuchTimePasses(t *testing.T) {
	_, client := newTestRedis(t)
	limiter := NewRateLimiter(client, 2, 1.0, false)
	clock := &fakeClock{now: time.Now()}
	limiter.Now = clock.Now

	clock.Advance(1 * time.Hour) // a fresh key: nothing to cap yet, but exercises the code path

	allowedCount := 0
	for i := 0; i < 5; i++ {
		allowed, _, err := limiter.Allow(context.Background(), "cap-key")
		if err != nil {
			t.Fatalf("request %d: unexpected error: %v", i, err)
		}
		if allowed {
			allowedCount++
		}
	}
	if allowedCount != 2 {
		t.Fatalf("expected exactly 2 requests allowed (bucket capacity), got %d", allowedCount)
	}
}

// Redis-down behavior: this gateway is configured fail-open (see cmd/gateway/main.go and
// docs/adr/0007-go-python-split.md), so an unreachable Redis must still ALLOW the request rather
// than making the whole gateway unavailable because of an unrelated infrastructure outage. The
// contrasting fail-closed policy is exercised in the next test, against the same RateLimiter type.
// newUnreachableTestRedis starts and immediately closes a miniredis instance, and returns a
// client pointed at its now-dead address with retries disabled and a short dial timeout, so a
// test proving "Redis is down" behavior fails fast instead of paying go-redis's default
// multi-attempt backoff (real time spent inside a dependency's own retry loop, not a
// services/gateway sleep call, but still worth avoiding for a fast test suite).
func newUnreachableTestRedis(t *testing.T) *redis.Client {
	t.Helper()
	mr := miniredis.RunT(t)
	addr := mr.Addr()
	mr.Close()
	return redis.NewClient(&redis.Options{
		Addr:        addr,
		MaxRetries:  -1,
		DialTimeout: 100 * time.Millisecond,
	})
}

func TestRateLimiter_FailsOpenWhenRedisIsUnreachable(t *testing.T) {
	client := newUnreachableTestRedis(t)

	limiter := NewRateLimiter(client, 5, 1.0, true /* failOpen */)
	allowed, _, err := limiter.Allow(context.Background(), "any-key")

	if err == nil {
		t.Fatal("expected an error when Redis is unreachable")
	}
	if !allowed {
		t.Fatal("expected fail-open to allow the request despite the Redis error")
	}
}

func TestRateLimiter_FailsClosedWhenConfiguredToAndRedisIsUnreachable(t *testing.T) {
	client := newUnreachableTestRedis(t)

	limiter := NewRateLimiter(client, 5, 1.0, false /* failClosed */)
	allowed, retryAfter, err := limiter.Allow(context.Background(), "any-key")

	if err == nil {
		t.Fatal("expected an error when Redis is unreachable")
	}
	if allowed {
		t.Fatal("expected fail-closed to deny the request when Redis is unreachable")
	}
	if retryAfter <= 0 {
		t.Fatal("expected a positive Retry-After even in the fail-closed/Redis-down case")
	}
}

func TestRateLimitMiddleware_Returns429WithRetryAfterWhenBucketIsEmpty(t *testing.T) {
	_, client := newTestRedis(t)
	limiter := NewRateLimiter(client, 1, 1.0, false)

	nextCalled := false
	handler := RateLimitMiddleware(limiter, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		nextCalled = true
		w.WriteHeader(http.StatusOK)
	}))

	// First request: consumes the only token, reaches the handler.
	req1 := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
	req1.RemoteAddr = "203.0.113.5:12345"
	rec1 := httptest.NewRecorder()
	handler.ServeHTTP(rec1, req1)
	if rec1.Code != http.StatusOK {
		t.Fatalf("expected the first request to succeed, got status %d", rec1.Code)
	}
	if !nextCalled {
		t.Fatal("expected the first request to reach the wrapped handler")
	}

	// Second request from the SAME IP: bucket is empty -- 429 immediately, next handler untouched.
	nextCalled = false
	req2 := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
	req2.RemoteAddr = "203.0.113.5:23456"
	rec2 := httptest.NewRecorder()
	handler.ServeHTTP(rec2, req2)

	if rec2.Code != http.StatusTooManyRequests {
		t.Fatalf("expected 429, got %d", rec2.Code)
	}
	if nextCalled {
		t.Fatal("expected the rate-limited request to never reach the wrapped handler")
	}
	if rec2.Header().Get("Retry-After") == "" {
		t.Fatal("expected a Retry-After header on a 429 response")
	}
}

// trustedTestCIDR is "10.0.0.0/8" -- covers the 10.0.0.1 RemoteAddr the tests below use as the
// stand-in for "a real reverse proxy sits at this address."
func trustedTestCIDR(t *testing.T) []*net.IPNet {
	t.Helper()
	_, cidr, err := net.ParseCIDR("10.0.0.0/8")
	if err != nil {
		t.Fatalf("failed to parse test CIDR: %v", err)
	}
	return []*net.IPNet{cidr}
}

// CORRECTED premise, stated plainly rather than silently: this test used to call
// RateLimitMiddleware with no trusted-proxy list at all and still expect X-Forwarded-For to be
// honored -- which was exactly the bypass bug (any caller could pick its own bucket per request by
// sending whatever X-Forwarded-For it liked, with no check on who was actually making the TCP
// connection). This is a correction to that wrong premise, not a weakening: the test now
// configures 10.0.0.1 (the RemoteAddr both requests use) as a TRUSTED peer, which is the one
// condition under which honoring X-Forwarded-For is supposed to happen at all, and the assertion
// itself (two different X-Forwarded-For values get two independent buckets) is unchanged.
func TestRateLimitMiddleware_HonorsXForwardedForWhenThePeerIsATrustedProxy(t *testing.T) {
	_, client := newTestRedis(t)
	limiter := NewRateLimiter(client, 1, 1.0, false)
	handler := RateLimitMiddleware(limiter, trustedTestCIDR(t))(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	// Same RemoteAddr (a trusted proxy), but DIFFERENT X-Forwarded-For first entries -- must be
	// treated as two distinct clients, each with their own bucket.
	req1 := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
	req1.RemoteAddr = "10.0.0.1:1"
	req1.Header.Set("X-Forwarded-For", "198.51.100.9, 10.0.0.1")
	rec1 := httptest.NewRecorder()
	handler.ServeHTTP(rec1, req1)
	if rec1.Code != http.StatusOK {
		t.Fatalf("expected client 198.51.100.9's first request to succeed, got %d", rec1.Code)
	}

	req2 := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
	req2.RemoteAddr = "10.0.0.1:2"
	req2.Header.Set("X-Forwarded-For", "198.51.100.10, 10.0.0.1")
	rec2 := httptest.NewRecorder()
	handler.ServeHTTP(rec2, req2)
	if rec2.Code != http.StatusOK {
		t.Fatalf("expected a DIFFERENT client (198.51.100.10) to have its own untouched bucket, got %d", rec2.Code)
	}
}

// THE BUG THIS PHASE FOUND: with no trusted-proxy list configured (the default -- see
// internal/config/config.go's TrustedProxyCIDRs), an untrusted peer must NEVER get to pick its own
// rate-limit bucket by sending a different X-Forwarded-For value on every request. Confirmed
// against a running gateway before this fix: a client that should have been 429'd after exhausting
// a 20-token bucket got ten further 200s simply by rotating a fabricated X-Forwarded-For value
// once per request; this test reproduces exactly that shape (a distinct XFF value per request) and
// asserts the SAME peer's bucket is what actually governs -- a burst that should exhaust the
// bucket does, no matter what X-Forwarded-For claims.
func TestRateLimitMiddleware_IgnoresXForwardedForWhenThePeerIsNotTrusted(t *testing.T) {
	_, client := newTestRedis(t)
	limiter := NewRateLimiter(client, 3, 1.0, false)
	// No trusted proxies configured -- nil, the actual default in cmd/gateway/main.go for the
	// docker-compose deployment (8080 published directly, nothing in front of it).
	handler := RateLimitMiddleware(limiter, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	const peer = "203.0.113.77:54321"
	var codes []int
	for i := 0; i < 5; i++ {
		req := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
		req.RemoteAddr = peer
		// A distinct, fabricated X-Forwarded-For value on every single request -- if this were
		// honored, each request would land in its own fresh bucket and none would ever be denied.
		req.Header.Set("X-Forwarded-For", fmt.Sprintf("10.9.9.%d", i+1))
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		codes = append(codes, rec.Code)
	}

	want := []int{
		http.StatusOK, http.StatusOK, http.StatusOK, // capacity 3
		http.StatusTooManyRequests, http.StatusTooManyRequests, // bucket now empty
	}
	for i, code := range codes {
		if code != want[i] {
			t.Fatalf("request %d: expected %d, got %d (full sequence: %v) -- an untrusted "+
				"X-Forwarded-For must never grant a fresh bucket", i, want[i], code, codes)
		}
	}
}

// A garbage X-Forwarded-For value from a TRUSTED peer must not be used as a bucket key verbatim --
// otherwise a misbehaving (or compromised) trusted proxy could still mint unbounded distinct Redis
// keys, one per garbage string it forwards. It must fall back to the peer's own address instead.
func TestRateLimitMiddleware_FallsBackToPeerAddressWhenATrustedProxysXFFDoesNotParseAsAnIP(t *testing.T) {
	_, client := newTestRedis(t)
	limiter := NewRateLimiter(client, 1, 1.0, false)
	handler := RateLimitMiddleware(limiter, trustedTestCIDR(t))(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	makeRequest := func(xff string) int {
		req := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
		req.RemoteAddr = "10.0.0.1:1"
		if xff != "" {
			req.Header.Set("X-Forwarded-For", xff)
		}
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		return rec.Code
	}

	if code := makeRequest("not-an-ip-address"); code != http.StatusOK {
		t.Fatalf("expected the first request (garbage XFF, falls back to peer) to succeed, got %d", code)
	}
	// Second request, same trusted peer, a DIFFERENT garbage value -- if garbage values were used
	// verbatim as bucket keys this would get a fresh bucket; falling back to the peer address
	// means it must instead hit the SAME (now exhausted, capacity 1) bucket.
	if code := makeRequest("also-not-an-ip"); code != http.StatusTooManyRequests {
		t.Fatalf("expected the second request (different garbage XFF, same peer) to be denied, got %d", code)
	}
}
