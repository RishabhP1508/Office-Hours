# 0016. Rate limiter: in-process fallback, never fail-open

## Context

A red-team pass against production (2026-09-07) measured this against the live gateway:

```
35  concurrent POST /v1/query/stream  -> {200: 35}
60  concurrent GET  /v1/sources/status -> {200: 60}   no Retry-After on any response
120 concurrent GET  /v1/sources/status -> {200: 120}
```

180 requests, zero 429s, against `RATE_LIMIT_BUCKET_CAPACITY=20`. Tracing it: `cmd/gateway/main.go`
built the rate limiter with `failOpen = true`; `internal/middleware/ratelimit.go`'s `Allow` returned
`l.failOpen` on any Redis error; `internal/config/config.go` defaulted `REDIS_URL` to
`redis://localhost:6379`, an address with no Redis behind it in production (Fly, Upstash). The three
facts together meant the rate limiter had never actually been enforcing anything in production, and
nothing surfaced that -- no log line distinguishable from routine traffic, no metric, no failed
health check.

## Decision

Three changes, together:

1. **`REDIS_URL` has no default.** An unset value is a startup error (`config.Load` returns an
   error; `cmd/gateway/main.go` calls `log.Fatalf` on it), not a silent fallback to an address that
   does not exist in production.
2. **Redis reachability is verified at startup** with a bounded ping (`PingRedisWithRetries`: 3
   attempts, 2s each, no `time.Sleep` between them -- each failed attempt's own dial timeout already
   spends real wall-clock time before the next attempt starts). The outcome is always logged
   explicitly, at ERROR on failure. This does not block startup: a failing ping does not stop the
   gateway from serving traffic, it only means traffic is served in the degraded mode below from the
   first request onward.
3. **`RateLimiter.Allow` never returns an unconditional true or false on a Redis error.** It falls
   back to `localBucketStore`, an in-process token bucket with the same capacity and refill as the
   Redis one, keyed the same way. Every fallback activation increments a counter
   (`FallbackActivations`), sets span attributes (`ratelimit.degraded`, `ratelimit.mode`,
   `ratelimit.redis_error`) on the active trace, and logs at WARN with the real Redis error -- never
   swallowed, never replaced with a generic message.

The old `failOpen bool` constructor argument is gone entirely. There is no longer a choice between
"fail open" and "fail closed" to make anywhere in this codebase; `Allow` always limits, one way or
the other.

## Tradeoff

The in-process bucket is weaker than the shared Redis bucket in exactly one way: it limits per
gateway instance, not globally across every instance behind a load balancer. A caller who can spread
requests across N gateway instances during a Redis outage gets N times the effective capacity. This
is accepted and documented, not hidden: it is still a real, finite bound (never "no limiting"), it
only ever applies during a Redis outage (a comparatively rare condition this project's own uptime
targets treat as transient), and the degradation is visible the moment it starts (log line, counter,
span attribute) rather than something that has to be inferred from an absence of 429s the way the
original defect was.

The in-process bucket's own memory also grows by one entry per distinct client key seen during an
outage, with no eviction. This is accepted for the same reason: the fallback exists for transient
outages, not as a general-purpose Redis replacement, and a long enough outage to make this matter is
already a bigger operational problem than this bucket's memory footprint.

## Alternatives considered

**Keep fail-open, add alerting.** Rejected: an alert fires after the fact, on a metric derived from
the very telemetry a silent fail-open never emits in the first place (the original defect had zero
signal to alert on). The fix has to close the gap at the source, not add a monitor for the gap that
should not exist.

**Switch to fail-closed.** Rejected explicitly by the same finding that identified fail-open as
wrong: fail-closed takes the entire gateway down on a transient Redis blip (a dependency this project
does not otherwise consider load-bearing for basic service availability), trading "no rate limiting"
for "no service at all." Neither extreme is acceptable for this product's actual risk profile: abuse
protection matters, but not more than the service itself staying up.

**A circuit breaker that opens fully after N consecutive failures.** Considered and rejected as
unnecessary complexity for this project's actual scale: the in-process fallback bucket already
degrades gracefully on every single failed Redis call, with no separate open/half-open/closed state
machine to reason about, test, or get wrong under concurrent access.
