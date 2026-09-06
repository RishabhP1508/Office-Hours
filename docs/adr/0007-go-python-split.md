# 0007: The Go gateway sits at the edge; the Python orchestrator keeps doing everything else

## Context

Through Phase 6, the browser called the orchestrator directly. Nothing stood between a request
and the database, the embedder, and the LLM: no rate limit, no PII redaction, no independent
timeout on the upstream call. CLAUDE.md fixed this at Phase 7: a Go gateway becomes the only thing
the browser talks to, and it owns four things the orchestrator never did -- a Redis token bucket,
PII redaction on the request body, an upstream timeout, and trace propagation.

Before writing any Go, the orchestrator was checked for what CLAUDE.md's "move rate limiting and
caching into the gateway" implied already existed. It does not: `grep`ing `services/orchestrator/app`
for rate limiting or caching turns up `functools.lru_cache` on `get_settings()` (an in-process
settings cache, not a request cache), `RobotsCache`/`HostRateLimiter` in `ingest.py`/`recrawl.py`
(these rate-limit and cache fetches of *government source pages during crawling*, unrelated to
`/query` traffic), and a `Cache-Control: no-cache` response header on the SSE stream (a plain HTTP
header, not application caching). There is no `app/cache.py` (a Phase 8 file that does not exist
yet) and no rate-limiting middleware anywhere in `app/main.py`. So the rate limiter built here is
new code, not a move, and Phase 8's semantic cache stays exactly as out of scope as it already was
-- this ADR does not invent one to fill a gap that turned out not to exist.

## Decision

### Why Go at the edge and Python everywhere else

The orchestrator's work is IO-bound waiting: a database round trip, an embedding call, an LLM
generation call that can run 20-140 seconds. Python's ecosystem (FastAPI, psycopg, httpx, the
RAGAS/LangGraph tooling the eval and refresh jobs already depend on) is the reason the orchestrator
is there, and none of that changes at the edge.

The edge's job is different: hold open many concurrent connections, decide in microseconds whether
a request gets a token, strip a few known-shaped substrings out of a JSON body, and forward bytes
through unbuffered. That is exactly the shape of work a goroutine-per-connection runtime is built
for, and it is work with no reason to touch a database, an embedder, or an LLM at all. Putting it in
Go keeps the edge a thin, fast, dependency-light layer instead of adding queueing and body-rewriting
logic to the same process that is already the slowest, most resource-heavy part of the system.

### Why an empty bucket returns 429 instead of waiting

A token bucket exists to say no to a request that would let one client crowd out the rest. Making
that request wait (parking a goroutine until a token frees up) turns a decision that is already known
-- there is no token right now -- into a held connection that ties up a file descriptor and a
goroutine for no benefit: the answer does not change by waiting, only when it is delivered does. An
empty bucket is a fact about the present moment, and CLAUDE.md is explicit that a known answer
should be returned, not waited on. Returning 429 immediately, with `Retry-After` telling the caller
exactly how long until a token accrues, gives the caller a concrete number to act on (retry, back
off, or tell a person to wait) instead of a connection that just sits there.

### Why no `time.Sleep` anywhere in `services/gateway`

A gateway's whole value is holding many concurrent connections cheaply. `time.Sleep` inside a
request path blocks the goroutine handling that request for a fixed duration regardless of what
actually happens during that time -- it cannot be interrupted by the event it is supposedly waiting
for (a response arriving, a client disconnecting), so it either wastes time past that event or, worse,
becomes the retry mechanism for a slow upstream and turns a slow dependency into what looks like a
gateway outage. Every wait in this codebase is instead a wait on the actual event: a channel receive
(`<-ctx.Done()`), a context deadline (`context.WithTimeout`, `context.WithCancel` plus
`time.AfterFunc`), or Redis's own atomicity (one Lua script, not a sleep-and-retry loop around two
round trips). `grep -rn "time.Sleep" services/gateway` returns nothing, checked as part of this
phase's verification, and it stays part of every future check of this package.

### The 15-second timeout, and why the SSE route needs a different bound

CLAUDE.md's `context.WithTimeout(ctx, 15s)` mandate is written for a request that returns once: a
database query, a single JSON response. `internal/middleware/timeout.go`'s `Timeout` middleware
applies exactly that, unmodified, to `/v1/query` and `/v1/sources/status` -- both return one JSON
body, and 15 seconds bounding the whole call is the right bound for a call that should never
legitimately run longer.

`/v1/query/stream` cannot use the same bound the same way. A real query against the local generator
takes 20 to 140 seconds end to end (services/orchestrator/app/config.py's own comment on
`LLM_TIMEOUT_SECONDS` says as much), so wrapping the whole streamed call in a 15-second
`context.WithTimeout` would cut off nearly every real answer partway through -- repointing the
frontend at this gateway would be a functional regression, not neutral. But the 15-second bound is
still protecting against something real on this route: an orchestrator that is hung or unreachable
and never responds at all. The distinction that matters is time to first byte (how long until the
response's headers arrive) versus total stream duration (how long the body takes to finish arriving)
-- the first is bounded by whether the orchestrator is even alive and answering, which genuinely
should not take more than 15 seconds; the second is bounded by how long the LLM takes to generate,
which genuinely can take over a minute and is not this gateway's problem to bound.

`internal/middleware/timeout.go`'s `TimeToFirstByte` implements exactly that split: a
`context.WithCancelCause` derived from the request's own context, with a `time.AfterFunc(timeout,
cancel)` racing against the upstream call. The cause is what a plain `context.WithCancel` would not
give: a context canceled by this timer and a context canceled because the browser disconnected both
report the same generic `context.Canceled` to anything that only calls `ctx.Err()`, so
`context.WithCancelCause` (cancelling with a specific `ErrTimeToFirstByteExceeded`) is what lets
`ProxyStream` tell them apart afterward via `context.Cause(ctx)` and report a real timeout as 504
rather than the same 502 a disconnected client would be indistinguishable from -- a distinction this
phase's own live testing caught missing on the first pass, before the cause was added.
`internal/proxy/proxy.go`'s `ProxyStream` calls `stop()` (the timer's `Stop`) the instant `client.Do`
returns -- success or failure, it does not matter which, only that the wait for headers is over --
and from that point on the context is bounded only by the underlying request's own context (which
itself only ends if the browser disconnects, canceling `r.Context()`, or the orchestrator closes the
stream). A stream that never sends so much as an HTTP status line within 15 seconds is caught; a
stream that answers promptly and then spends 90 seconds
generating tokens is not.

### Redis-down behavior: fail open

When Redis itself cannot be reached, `RateLimiter.Allow` has two honest choices: let the request
through (fail open) or treat it as if the bucket were empty (fail closed). This gateway fails open,
configured that way in `cmd/gateway/main.go` and covered by both branches in
`internal/middleware/ratelimit_test.go` so the choice is a real, tested behavior rather than an
unexercised code path.

The reasoning is about what this product actually is: an information tool for a population that
sometimes has a real filing deadline bearing down on it, running as a single gateway instance with
no payment flow and no per-request cost that turns catastrophic if it briefly runs unmetered. A rate
limiter's job is to stop one client from crowding out the rest and from running up API/compute cost
unbounded; a Redis outage is rare, and during it, failing closed would take the entire product down
for every legitimate user because of an unrelated infrastructure component, which is a worse outcome
than a short, rare window with no rate limiting at all. Fail open trades a small amount of abuse
exposure during a rare outage for availability during that same outage, and for this product's risk
profile that is the right side to err on.

## Tradeoff

Fail open means a coordinated abuse attempt that also manages to take Redis down (or simply arrives
during an unrelated Redis outage) faces no rate limiting at all for the duration. That risk is
accepted deliberately, not overlooked: the alternative (fail closed) means an ordinary Redis restart
or network blip makes the whole product unavailable to every legitimate user, which is a worse and
far more likely failure mode for a single-instance educational tool than a rare, brief gap in abuse
protection.

The time-to-first-byte split also means the 15-second bound genuinely does not protect against a
slow-but-alive upstream on the streaming route -- an orchestrator that answers promptly but then
hangs 90% of the way through generating a response is not caught by this gateway's timeout at all,
only by the browser eventually giving up or the person closing the tab. That gap is deliberate: the
alternative (a hard cap on stream duration) would make this gateway less useful than calling the
orchestrator directly, for a product where the whole value is answering a real question, however long
that legitimately takes.

## Alternatives considered

- **One timeout duration and shape for every route, including the stream.** Rejected: this either
  breaks every real streamed answer (a 15s cap on the whole call) or makes the "hung orchestrator"
  case at `/v1/query`/`/v1/sources/status` needlessly generous by reusing the SSE route's looser
  bound everywhere. The two routes protect against genuinely different failure shapes and need
  genuinely different bounds.
- **A queueing rate limiter (hold the request until a token frees up, bounded by a short wait).**
  Rejected: CLAUDE.md is explicit that an empty bucket is a known answer and should be returned, not
  waited on, and a bounded wait is still a wait -- it still ties up a goroutine and a connection for
  a decision that does not change during that wait.
- **Fail closed on a Redis outage.** Rejected for this product's risk profile (see Decision above);
  recorded here because it is the sound alternative for a system whose per-request cost or abuse
  exposure is high enough that unmetered traffic during an outage is the worse failure.
- **A read-modify-write rate limiter (GET the bucket, compute in Go, SET it back).** Rejected: two
  round trips are not atomic against a second concurrent request for the same key, and closing that
  race with a Redis transaction (`WATCH`/`MULTI`/`EXEC`) is strictly more code and more round trips
  than one `EVAL` that does the whole read-refill-decide-write sequence server-side. See
  `internal/middleware/ratelimit.lua`.
