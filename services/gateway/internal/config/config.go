// Package config loads gateway configuration from environment variables. Every value the gateway
// needs at runtime is read from here, and nowhere else: no hardcoded hosts, no secrets, so the
// same binary runs in docker-compose (service hostnames) and anywhere else (real hostnames)
// without a code change.
package config

import (
	"errors"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config is every environment-driven setting the gateway reads once at startup.
type Config struct {
	// ListenAddr is what http.ListenAndServe binds to, e.g. ":8080".
	ListenAddr string

	// OrchestratorURL is the base URL of the Python orchestrator this gateway proxies to (e.g.
	// "http://orchestrator:8000" inside docker compose). Never has a trailing slash.
	OrchestratorURL string

	// RedisURL is the connection string for the token-bucket rate limiter's Redis store. NO
	// DEFAULT (see Load, below): an unset REDIS_URL is a startup error, not a silent fallback to
	// "redis://localhost:6379" -- that address does not exist in production (Fly, Upstash), and a
	// silent fallback to it was the exact mechanism of a real red-team finding: 180 requests fired
	// at production with RATE_LIMIT_BUCKET_CAPACITY=20 got zero 429s, because Redis was
	// unreachable at that address and the OLD code's fail-open policy (see
	// internal/middleware/ratelimit.go) let every request through with nothing surfacing the
	// degradation. Removing the default cannot by itself fix a Redis OUTAGE after startup (that is
	// what ratelimit.go's in-process fallback bucket is for) -- it fixes the narrower, sharper bug
	// of a MISSING config value being indistinguishable from a real, working Redis at the
	// well-known local address.
	RedisURL string

	// RateLimitCapacity is the token bucket's maximum size (burst allowance), per client IP.
	RateLimitCapacity int
	// RateLimitRefillPerSecond is how many tokens the bucket refills per second, per client IP.
	RateLimitRefillPerSecond float64

	// UpstreamTimeout bounds every non-streaming upstream call in full
	// (internal/middleware/timeout.go's Timeout middleware), and bounds time-to-first-byte ONLY
	// for the SSE stream route (internal/middleware/timeout.go's TimeToFirstByte, used by
	// internal/proxy/proxy.go's ProxyStream) -- see docs/adr/0007-go-python-split.md for why the
	// same duration means two different things on two different routes. Defaults to 15s, per
	// CLAUDE.md's context.WithTimeout(ctx, 15s) mandate; an env override that parses to zero,
	// negative, or garbage falls back to the default rather than disabling the bound, so the
	// override can change the value but can never remove it.
	UpstreamTimeout time.Duration

	// AllowedOrigins is the set of origins the gateway answers CORS preflight for -- the browser
	// talks to the gateway now, not directly to the orchestrator (the orchestrator's own CORS
	// middleware is untouched; it still answers direct callers, e.g. curl during development).
	AllowedOrigins []string

	// SessionHashSalt keys the HMAC-SHA256 hash internal/middleware/session.go computes from each
	// request's own client address, forwarded to the orchestrator as the X-Office-Hours-Session-Hash
	// header (services/orchestrator/app/main.py::_client_session_hash reads it) so GET /usage's
	// distinct_sessions count is real even though every request's own TCP peer, from the
	// orchestrator's point of view, is this gateway rather than the original caller (Phase 8 round
	// 4). SHOULD be the SAME value as the orchestrator's own SESSION_HASH_SALT (app/config.py) --
	// that is what makes a client hash to the same session whether it reaches the orchestrator
	// through this gateway or directly; a mismatch does not break per-path distinct-session
	// counting, it only loses that cross-path consistency (see session.go's own doc comment). The
	// default below matches Settings.SESSION_HASH_SALT's own dev-only default exactly, so a fresh
	// `docker compose up` on both services already agrees out of the box; production MUST override
	// this to a real, random secret on BOTH services, the same as every other secret in this
	// project.
	SessionHashSalt string

	// TrustedProxyCIDRs is who the rate limiter (internal/middleware/ratelimit.go's clientIP) will
	// believe an X-Forwarded-For header from. Defaults to EMPTY: with nothing configured, the
	// header is never trusted and every request is keyed on its own TCP peer address, which is the
	// correct behavior for how this gateway is actually deployed today (docker-compose publishes
	// 8080 directly, with no reverse proxy in front of it) -- an unconditionally-trusted
	// X-Forwarded-For lets any caller pick their own rate-limit bucket per request simply by
	// sending a different header value, which is not a hardening gap, it is the bucket not
	// existing. The header becomes trusted only when an operator deliberately puts a real proxy in
	// front and says so here.
	TrustedProxyCIDRs []*net.IPNet

	// OTLPEndpoint is where spans are exported: OTLP/HTTP, the same grafana/otel-lgtm endpoint the
	// orchestrator already exports to (services/orchestrator/app/telemetry.py).
	OTLPEndpoint string
	// ServiceName tags every span this process emits. Deliberately distinct from the
	// orchestrator's own "office-hours-orchestrator" so a trace visibly contains spans from both
	// services rather than one service's spans being misattributed to the other.
	ServiceName string
}

const (
	defaultListenAddr            = ":8080"
	defaultOrchestratorURL       = "http://localhost:8000"
	defaultRateLimitCapacity     = 20
	defaultRateLimitRefillPerSec = 1.0
	defaultUpstreamTimeout       = 15 * time.Second
	defaultAllowedOrigins        = "http://localhost:3000"
	defaultOTLPEndpoint          = "http://localhost:4318"
	defaultServiceName           = "office-hours-gateway"
	// Matches services/orchestrator/app/config.py's Settings.SESSION_HASH_SALT dev-only default
	// exactly -- see SessionHashSalt's own comment above for why that match matters.
	defaultSessionHashSalt = "office-hours-dev-salt-change-in-production"
)

// ErrRedisURLRequired is returned by Load when REDIS_URL is unset or empty. Exported so a caller
// (or a test) can match on it specifically, rather than string-matching the message.
var ErrRedisURLRequired = errors.New(
	"REDIS_URL is required and must be set explicitly -- there is no default. An unset value used " +
		"to fall back to \"redis://localhost:6379\", an address that does not exist in production " +
		"(Fly, Upstash) -- see RedisURL's own doc comment for the real incident that fallback " +
		"caused: 180 requests against production, zero 429s, because Redis was silently unreachable " +
		"and the rate limiter's old fail-open policy let everything through with no signal that it " +
		"was happening. Set REDIS_URL (docker-compose.yml already does, for local dev; production " +
		"sets it as a Fly secret, see infra/deploy/fly.gateway.toml)",
)

// Load reads Config from the process environment, applying the defaults above wherever a variable
// is unset, empty, or fails to parse -- EXCEPT RedisURL, which has no default at all (see its own
// doc comment and ErrRedisURLRequired above): an unset REDIS_URL is a startup error, returned here,
// never a silent fallback.
func Load() (Config, error) {
	redisURL := os.Getenv("REDIS_URL")
	if redisURL == "" {
		return Config{}, ErrRedisURLRequired
	}

	return Config{
		ListenAddr:               envOr("GATEWAY_LISTEN_ADDR", defaultListenAddr),
		OrchestratorURL:          strings.TrimRight(envOr("ORCHESTRATOR_URL", defaultOrchestratorURL), "/"),
		RedisURL:                 redisURL,
		RateLimitCapacity:        envPositiveIntOr("RATE_LIMIT_BUCKET_CAPACITY", defaultRateLimitCapacity),
		RateLimitRefillPerSecond: envPositiveFloatOr("RATE_LIMIT_REFILL_PER_SECOND", defaultRateLimitRefillPerSec),
		UpstreamTimeout:          envTimeoutSecondsOr("UPSTREAM_TIMEOUT_SECONDS", defaultUpstreamTimeout),
		AllowedOrigins:           splitOrigins(envOr("ALLOWED_ORIGINS", defaultAllowedOrigins)),
		SessionHashSalt:          envOr("SESSION_HASH_SALT", defaultSessionHashSalt),
		TrustedProxyCIDRs:        parseTrustedProxyCIDRs(envOr("TRUSTED_PROXY_CIDRS", "")),
		OTLPEndpoint:             envOr("OTEL_EXPORTER_OTLP_ENDPOINT", defaultOTLPEndpoint),
		ServiceName:              envOr("OTEL_SERVICE_NAME", defaultServiceName),
	}, nil
}

// parseTrustedProxyCIDRs parses a comma-separated list of CIDRs (e.g. "10.0.0.0/8,172.16.0.0/12").
// Empty input (the default) returns nil -- an empty trust list, not a list that trusts everything.
// An entry that fails to parse as a CIDR is skipped rather than failing the whole process: the
// safe direction for a malformed trusted-proxy entry is to grant LESS trust, not more, so a typo
// here degrades to "that entry is not trusted" rather than silently trusting every peer.
func parseTrustedProxyCIDRs(raw string) []*net.IPNet {
	if raw == "" {
		return nil
	}
	parts := strings.Split(raw, ",")
	cidrs := make([]*net.IPNet, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		if _, ipNet, err := net.ParseCIDR(p); err == nil {
			cidrs = append(cidrs, ipNet)
		}
	}
	return cidrs
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envPositiveIntOr(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return fallback
	}
	return n
}

func envPositiveFloatOr(key string, fallback float64) float64 {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil || f <= 0 {
		return fallback
	}
	return f
}

// envTimeoutSecondsOr reads an env var as a whole or fractional number of seconds. The bound
// cannot be removed by an env override: a missing, unparseable, zero, or negative value falls
// back to `fallback` rather than ever producing a zero/unbounded timeout.
func envTimeoutSecondsOr(key string, fallback time.Duration) time.Duration {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil || f <= 0 {
		return fallback
	}
	return time.Duration(f * float64(time.Second))
}

func splitOrigins(raw string) []string {
	parts := strings.Split(raw, ",")
	origins := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			origins = append(origins, p)
		}
	}
	return origins
}
