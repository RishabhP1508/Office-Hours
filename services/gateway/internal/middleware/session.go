package middleware

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net"
	"net/http"
)

// SessionHeaderName is the header this gateway sets on every request forwarded to the
// orchestrator, carrying an opaque, salted hash of the requesting client's own address --
// services/orchestrator/app/main.py::_client_session_hash reads it to make GET /usage's
// distinct_sessions count real behind this gateway.
const SessionHeaderName = "X-Office-Hours-Session-Hash"

// SessionHashMiddleware sets SessionHeaderName to hex(HMAC-SHA256(clientIP, salt)) on every
// request BEFORE it is ever forwarded upstream -- internal/proxy/proxy.go's copyRequestHeaders
// carries every request header through unchanged, this one included, so no change to proxy.go
// itself is needed for the header to actually reach the orchestrator.
//
// WHY A HASH, NOT THE RAW ADDRESS (Phase 8 round 4): before this middleware existed, the
// orchestrator had no way at all to tell two different callers apart once they were both behind
// this gateway -- services/orchestrator/app/main.py::_client_session_hash fell back to hashing
// THIS GATEWAY's own address (every request's TCP peer, from the orchestrator's point of view),
// so GET /usage's distinct_sessions sat at 1 forever in production. The fix needs SOME
// client-identifying value to cross the gateway/orchestrator boundary -- but forwarding the RAW
// address in a new header would introduce a brand new category of information crossing that
// boundary that never crossed it before (this gateway's own PII redaction, internal/middleware/
// pii.go, exists for exactly this reason: minimize what crosses this boundary, never widen it).
// Hashing HERE, with a salt this gateway never sends anywhere, keeps the boundary exactly as
// closed as it always was: the orchestrator receives an opaque value it cannot turn back into an
// address, the same one-way property services/orchestrator/app/usage.py::hash_session_identifier
// already gives a direct caller that never goes through this gateway at all.
//
// `salt` should be the SAME value as the orchestrator's own SESSION_HASH_SALT (see
// internal/config/config.go's SessionHashSalt) -- that is what makes one real client hash to the
// SAME session whether it reaches the orchestrator through this gateway or (e.g. local dev)
// directly. A mismatched salt on either side does not break distinct-session counting on its own
// path; it only loses that cross-path consistency.
//
// clientIP is REUSED from ratelimit.go, not re-derived: it is the exact same "trust the TCP peer
// unless an explicitly configured proxy CIDR says otherwise" logic the rate limiter is already
// keyed on (internal/config/config.go's TrustedProxyCIDRs), so this hash and the rate limiter
// always agree on who the caller actually is.
func SessionHashMiddleware(salt string, trustedProxyCIDRs []*net.IPNet) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ip := clientIP(r, trustedProxyCIDRs)
			mac := hmac.New(sha256.New, []byte(salt))
			mac.Write([]byte(ip))
			r.Header.Set(SessionHeaderName, hex.EncodeToString(mac.Sum(nil)))
			next.ServeHTTP(w, r)
		})
	}
}
