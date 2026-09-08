package config

import (
	"errors"
	"testing"
)

// The real defect this guards: REDIS_URL unset used to silently fall back to
// "redis://localhost:6379", an address that does not exist in production -- Load must instead
// fail loudly with a specific, named error, never construct a Config with a guessed default.
func TestLoad_MissingRedisURLIsAStartupErrorNotASilentDefault(t *testing.T) {
	t.Setenv("REDIS_URL", "")

	cfg, err := Load()

	if err == nil {
		t.Fatal("expected an error when REDIS_URL is unset, got nil")
	}
	if !errors.Is(err, ErrRedisURLRequired) {
		t.Fatalf("expected ErrRedisURLRequired, got: %v", err)
	}
	if cfg.RedisURL != "" {
		t.Fatalf("expected a zero-value Config on error, got RedisURL=%q", cfg.RedisURL)
	}
	if cfg.RedisURL == "redis://localhost:6379" {
		t.Fatal("Load must never fall back to redis://localhost:6379 -- that is the exact defect")
	}
}

func TestLoad_RedisURLSetIsUsedVerbatimWithNoDefaultSubstitution(t *testing.T) {
	t.Setenv("REDIS_URL", "redis://some-real-host:6379")

	cfg, err := Load()

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.RedisURL != "redis://some-real-host:6379" {
		t.Fatalf("RedisURL = %q, want the value REDIS_URL was actually set to", cfg.RedisURL)
	}
}

// Every OTHER setting must keep falling back to its own documented default when unset --
// REDIS_URL's own strictness must not spill over into surprising the rest of Load's contract.
func TestLoad_OtherSettingsStillFallBackToDefaultsWhenUnset(t *testing.T) {
	t.Setenv("REDIS_URL", "redis://some-real-host:6379")
	t.Setenv("GATEWAY_LISTEN_ADDR", "")
	t.Setenv("RATE_LIMIT_BUCKET_CAPACITY", "")

	cfg, err := Load()

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.ListenAddr != defaultListenAddr {
		t.Fatalf("ListenAddr = %q, want default %q", cfg.ListenAddr, defaultListenAddr)
	}
	if cfg.RateLimitCapacity != defaultRateLimitCapacity {
		t.Fatalf("RateLimitCapacity = %d, want default %d", cfg.RateLimitCapacity, defaultRateLimitCapacity)
	}
}
