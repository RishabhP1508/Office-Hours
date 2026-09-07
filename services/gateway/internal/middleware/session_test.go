package middleware

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"testing"
)

func hmacHex(t *testing.T, value, salt string) string {
	t.Helper()
	mac := hmac.New(sha256.New, []byte(salt))
	mac.Write([]byte(value))
	return hex.EncodeToString(mac.Sum(nil))
}

func TestSessionHashMiddleware_SetsAnHMACHeaderDerivedFromThePeerAddress(t *testing.T) {
	var gotHeader string
	handler := SessionHashMiddleware("test-salt", nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotHeader = r.Header.Get(SessionHeaderName)
		w.WriteHeader(http.StatusOK)
	}))

	req := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
	req.RemoteAddr = "203.0.113.5:12345"
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	want := hmacHex(t, "203.0.113.5", "test-salt")
	if gotHeader != want {
		t.Fatalf("expected %s header %q (HMAC of the peer address), got %q", SessionHeaderName, want, gotHeader)
	}
}

func TestSessionHashMiddleware_NeverForwardsTheRawAddress(t *testing.T) {
	var gotHeader string
	handler := SessionHashMiddleware("test-salt", nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotHeader = r.Header.Get(SessionHeaderName)
		w.WriteHeader(http.StatusOK)
	}))

	req := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
	req.RemoteAddr = "203.0.113.5:12345"
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if gotHeader == "203.0.113.5" {
		t.Fatal("the raw client address must never cross this boundary verbatim")
	}
	if len(gotHeader) != 64 {
		t.Fatalf("expected a 64-character hex SHA-256 digest, got %d characters: %q", len(gotHeader), gotHeader)
	}
}

func TestSessionHashMiddleware_SameAddressAlwaysHashesTheSame(t *testing.T) {
	handler := SessionHashMiddleware("test-salt", nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	hashOf := func(remoteAddr string) string {
		req := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
		req.RemoteAddr = remoteAddr
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		return req.Header.Get(SessionHeaderName)
	}

	first := hashOf("203.0.113.5:1")
	second := hashOf("203.0.113.5:2") // same IP, different ephemeral port
	if first != second {
		t.Fatalf("expected the same client IP to hash to the same session regardless of source port, got %q vs %q", first, second)
	}
}

func TestSessionHashMiddleware_DifferentAddressesHashDifferently(t *testing.T) {
	handler := SessionHashMiddleware("test-salt", nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	hashOf := func(remoteAddr string) string {
		req := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
		req.RemoteAddr = remoteAddr
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		return req.Header.Get(SessionHeaderName)
	}

	a := hashOf("203.0.113.5:1")
	b := hashOf("203.0.113.6:1")
	if a == b {
		t.Fatal("expected two different client IPs to hash to two different sessions")
	}
}

func TestSessionHashMiddleware_DifferentSaltsHashDifferently(t *testing.T) {
	hashWithSalt := func(salt string) string {
		var got string
		handler := SessionHashMiddleware(salt, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			got = r.Header.Get(SessionHeaderName)
			w.WriteHeader(http.StatusOK)
		}))
		req := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
		req.RemoteAddr = "203.0.113.5:1"
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		return got
	}

	if hashWithSalt("salt-one") == hashWithSalt("salt-two") {
		t.Fatal("expected different salts to produce different hashes for the same address")
	}
}

// Honors the same trusted-proxy/X-Forwarded-For logic the rate limiter is keyed on (clientIP,
// shared with ratelimit.go) -- an untrusted peer's X-Forwarded-For must be ignored here exactly as
// it is for rate limiting, so the two never disagree about who the caller is.
func TestSessionHashMiddleware_IgnoresXForwardedForFromAnUntrustedPeer(t *testing.T) {
	handler := SessionHashMiddleware("test-salt", nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	req := httptest.NewRequest(http.MethodPost, "/v1/query", nil)
	req.RemoteAddr = "203.0.113.5:1"
	req.Header.Set("X-Forwarded-For", "198.51.100.9")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	want := hmacHex(t, "203.0.113.5", "test-salt")
	if got := req.Header.Get(SessionHeaderName); got != want {
		t.Fatalf("expected the untrusted peer's OWN address to be hashed (%q), got %q", want, got)
	}
}
