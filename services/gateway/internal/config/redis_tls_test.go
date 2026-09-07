package config

// Upstash Redis is rediss:// (TLS). cmd/gateway/main.go calls redis.ParseURL(cfg.RedisURL)
// directly and trusts the returned *redis.Options as-is -- it does NOT independently inspect the
// URL scheme or build its own tls.Config. That is only safe if go-redis's own ParseURL already
// turns a "rediss://" scheme into a real, non-nil TLSConfig (with ServerName set to the host, so
// certificate verification actually checks against the right name) and leaves TLSConfig nil for a
// plain "redis://" URL. This test asserts exactly that, against the real go-redis library code
// this project depends on (github.com/redis/go-redis/v9, see go.mod) -- not a stand-in -- so a
// future go-redis upgrade that silently changed this behavior would break this test, not go
// unnoticed until it broke Upstash in production.

import (
	"testing"

	"github.com/redis/go-redis/v9"
)

func TestParseURL_RedissSchemeProducesNonNilTLSConfigWithServerNameSetToHost(t *testing.T) {
	opts, err := redis.ParseURL("rediss://user:pass@upstash-example.io:6379")
	if err != nil {
		t.Fatalf("ParseURL returned an error for a rediss:// URL: %v", err)
	}
	if opts.TLSConfig == nil {
		t.Fatal("ParseURL on a rediss:// URL produced a nil TLSConfig; Upstash requires TLS, and " +
			"cmd/gateway/main.go passes opts straight to redis.NewClient with no TLS logic of its " +
			"own, so a nil TLSConfig here means the gateway would try a plaintext connection to " +
			"Upstash and fail (or, worse, silently connect to a non-TLS endpoint elsewhere).")
	}
	if opts.TLSConfig.ServerName != "upstash-example.io" {
		t.Fatalf("TLSConfig.ServerName = %q, want %q (the host from the URL) -- an empty or wrong "+
			"ServerName would make TLS certificate verification check against the wrong name.",
			opts.TLSConfig.ServerName, "upstash-example.io")
	}
}

func TestParseURL_RedisSchemeProducesNilTLSConfig(t *testing.T) {
	opts, err := redis.ParseURL("redis://localhost:6379")
	if err != nil {
		t.Fatalf("ParseURL returned an error for a redis:// URL: %v", err)
	}
	if opts.TLSConfig != nil {
		t.Fatalf("ParseURL on a plain redis:// URL produced a non-nil TLSConfig (%+v); a local, "+
			"non-TLS Redis (docker-compose.yml's own redis:7-alpine service, REDIS_URL="+
			"redis://redis:6379) must not have TLS forced on it.", opts.TLSConfig)
	}
}

func TestParseURL_RedissSchemeWithDifferentHostUsesThatHostAsServerName(t *testing.T) {
	// Guards against the first test's assertion being a coincidence of one hardcoded host: the
	// ServerName must track WHATEVER host is in the URL, not a fixed string.
	opts, err := redis.ParseURL("rediss://default:secret@my-real-upstash-instance.upstash.io:6380")
	if err != nil {
		t.Fatalf("ParseURL returned an error: %v", err)
	}
	if opts.TLSConfig == nil {
		t.Fatal("expected a non-nil TLSConfig for a rediss:// URL")
	}
	if opts.TLSConfig.ServerName != "my-real-upstash-instance.upstash.io" {
		t.Fatalf("TLSConfig.ServerName = %q, want %q",
			opts.TLSConfig.ServerName, "my-real-upstash-instance.upstash.io")
	}
}
