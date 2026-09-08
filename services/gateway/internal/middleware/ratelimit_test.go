package middleware

import (
	"bytes"
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
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
	limiter := NewRateLimiter(client, 3, 1.0)

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
	limiter := NewRateLimiter(client, 2, 1.0)

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
	limiter := NewRateLimiter(client, 1, 1.0)

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
	limiter := NewRateLimiter(client, 1, 1.0)
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
	limiter := NewRateLimiter(client, 2, 1.0)
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

// Redis-down behavior (red-team fix, 2026-09-07, docs/adr/0016-rate-limiter-fallback-not-fail-
// open.md): an unreachable Redis must NEVER mean "allow every request" (the real production
// defect: 180 requests fired at a capacity-20 bucket with Redis unreachable got zero 429s) and
// must NEVER mean "deny every request either" (that takes the whole gateway down on a transient
// Redis blip). The in-process fallback bucket below is the one policy that is neither.
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

// THE CORRECTED PREMISE: this used to be TestRateLimiter_FailsOpenWhenRedisIsUnreachable, and
// asserted `allowed` was unconditionally true on a Redis error -- that IS the production defect
// (see the module-level comment above and RateLimiter's own doc comment), not a policy to keep
// testing as correct. It is replaced here with the real requirement: a single request against an
// unreachable Redis is governed by the in-process fallback bucket instead, which for a FRESH key
// starts full and therefore does allow the first request -- but (unlike the old policy) it is
// still counted against a real, finite bucket, proven by the burst test directly below this one.
func TestRateLimiter_FallsBackToInProcessBucketWhenRedisIsUnreachable(t *testing.T) {
	client := newUnreachableTestRedis(t)

	limiter := NewRateLimiter(client, 5, 1.0)
	allowed, _, err := limiter.Allow(context.Background(), "any-key")

	if err == nil {
		t.Fatal("expected an error when Redis is unreachable")
	}
	if !allowed {
		t.Fatal("expected the first request against a fresh fallback bucket (capacity 5) to be allowed")
	}
	if limiter.FallbackActivations.Load() != 1 {
		t.Fatalf("expected FallbackActivations == 1 after one Redis-unreachable call, got %d",
			limiter.FallbackActivations.Load())
	}
}

// THE ACTUAL DEFECT, REPRODUCED AND FIXED: with Redis unreachable, a burst of 30 requests against
// a capacity-20 bucket must NOT get 30 allows (the old fail-open behavior, and exactly the
// production incident this fix closes) -- the 21st request, and everything after it, must be
// 429'd by the in-process fallback bucket, the same as capacity-20 would behave against a healthy
// Redis.
func TestRateLimiter_FallbackBucketDeniesThe21stRequestInABurstOf30WhenRedisIsUnreachable(t *testing.T) {
	client := newUnreachableTestRedis(t)
	limiter := NewRateLimiter(client, 20, 1.0)
	clock := &fakeClock{now: time.Now()}
	limiter.Now = clock.Now // freeze time: no refill during the burst, so this isolates capacity

	var allowedCount int
	var firstDeniedAt = -1
	for i := 0; i < 30; i++ {
		allowed, retryAfter, err := limiter.Allow(context.Background(), "burst-key")
		if err == nil {
			t.Fatalf("request %d: expected a Redis error (Redis is unreachable), got nil", i)
		}
		if allowed {
			allowedCount++
		} else {
			if firstDeniedAt == -1 {
				firstDeniedAt = i
			}
			if retryAfter <= 0 {
				t.Fatalf("request %d: expected a positive Retry-After when denied, got %v", i, retryAfter)
			}
		}
	}

	if allowedCount != 20 {
		t.Fatalf("expected exactly 20 of 30 requests allowed (bucket capacity 20) with Redis "+
			"unreachable, got %d -- a fail-open bucket would allow all 30, which is the exact "+
			"production defect this test guards against", allowedCount)
	}
	if firstDeniedAt != 20 {
		t.Fatalf("expected the 21st request (index 20) to be the first denied, got index %d",
			firstDeniedAt)
	}
	if limiter.FallbackActivations.Load() != 30 {
		t.Fatalf("expected FallbackActivations == 30 (one per Allow call against unreachable "+
			"Redis), got %d", limiter.FallbackActivations.Load())
	}
}

// The fallback bucket refills using the exact same math and the exact same injected clock as the
// Redis path (RateLimiter.Now) -- never a real sleep.
func TestRateLimiter_FallbackBucketRefillsOverTimeUsingTheInjectedClockNotRealSleep(t *testing.T) {
	client := newUnreachableTestRedis(t)
	limiter := NewRateLimiter(client, 1, 1.0)
	clock := &fakeClock{now: time.Now()}
	limiter.Now = clock.Now

	allowed, _, _ := limiter.Allow(context.Background(), "fallback-refill-key")
	if !allowed {
		t.Fatal("expected the first request against a fresh fallback bucket to be allowed")
	}
	deniedImmediately, _, _ := limiter.Allow(context.Background(), "fallback-refill-key")
	if deniedImmediately {
		t.Fatal("expected the second request to be denied immediately, before any time passes")
	}

	clock.Advance(2 * time.Second)

	allowedAfterRefill, _, _ := limiter.Allow(context.Background(), "fallback-refill-key")
	if !allowedAfterRefill {
		t.Fatal("expected the fallback bucket to have refilled after advancing the clock 2s at " +
			"1 token/second")
	}
}

// Every fallback activation must log its real cause at WARN, never silently -- this is precisely
// the "silent fail-open" pattern CLAUDE.md's red-team round flagged (naming
// app/guardrails/classifier.py as one instance already); this test asserts the log line exists,
// names the real error, and is marked at a severity a log-scraping alert would actually catch.
func TestRateLimiter_FallbackActivationLogsAtWarnWithTheRealRedisError(t *testing.T) {
	var buf bytes.Buffer
	origOutput := log.Writer()
	origFlags := log.Flags()
	log.SetOutput(&buf)
	log.SetFlags(0)
	t.Cleanup(func() {
		log.SetOutput(origOutput)
		log.SetFlags(origFlags)
	})

	client := newUnreachableTestRedis(t)
	limiter := NewRateLimiter(client, 5, 1.0)
	_, _, err := limiter.Allow(context.Background(), "logged-key")
	if err == nil {
		t.Fatal("expected a real Redis error to log")
	}

	logged := buf.String()
	if !strings.Contains(logged, "WARN") {
		t.Fatalf("expected the fallback activation to be logged at WARN, got: %s", logged)
	}
	if !strings.Contains(logged, err.Error()) {
		t.Fatalf("expected the log line to contain the real Redis error %q, got: %s", err.Error(), logged)
	}
}

// THE FULL HTTP-LAYER REPRODUCTION of the production incident: with Redis unreachable, a burst of
// 30 requests through the actual RateLimitMiddleware (not just RateLimiter.Allow directly) against
// a capacity-20 bucket must still return exactly 20 200s and 10 429s, each 429 carrying a
// Retry-After header -- proving the fallback bucket governs real HTTP responses, not just the
// lower-level Allow return values.
func TestRateLimitMiddleware_Returns429sOnceFallbackCapacityIsExhaustedWhenRedisIsUnreachable(t *testing.T) {
	client := newUnreachableTestRedis(t)
	limiter := NewRateLimiter(client, 20, 1.0)
	clock := &fakeClock{now: time.Now()}
	limiter.Now = clock.Now // freeze time: isolates capacity from refill

	handler := RateLimitMiddleware(limiter, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	var okCount, tooManyCount int
	for i := 0; i < 30; i++ {
		req := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
		req.RemoteAddr = "198.51.100.42:12345"
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		switch rec.Code {
		case http.StatusOK:
			okCount++
		case http.StatusTooManyRequests:
			tooManyCount++
			if rec.Header().Get("Retry-After") == "" {
				t.Fatalf("request %d: expected a Retry-After header on a 429 response even with "+
					"Redis unreachable, got none", i)
			}
		default:
			t.Fatalf("request %d: unexpected status %d", i, rec.Code)
		}
	}

	if okCount != 20 {
		t.Fatalf("expected exactly 20 of 30 requests to succeed (bucket capacity 20) with Redis "+
			"unreachable, got %d -- the old fail-open behavior would let all 30 through", okCount)
	}
	if tooManyCount != 10 {
		t.Fatalf("expected exactly 10 of 30 requests to be 429'd, got %d", tooManyCount)
	}
}

func TestRateLimitMiddleware_Returns429WithRetryAfterWhenBucketIsEmpty(t *testing.T) {
	_, client := newTestRedis(t)
	limiter := NewRateLimiter(client, 1, 1.0)

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
	limiter := NewRateLimiter(client, 1, 1.0)
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
	limiter := NewRateLimiter(client, 3, 1.0)
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
	limiter := NewRateLimiter(client, 1, 1.0)
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
