-- Redis token bucket: refill computed from elapsed wall-clock time, entirely inside this ONE
-- atomic Lua script (EVAL runs a script to completion before serving any other command), so there
-- is no read-modify-write race across two round trips the way a GET-then-SET implementation from
-- the Go side would have.
--
-- KEYS[1] = the bucket's Redis key (one per client IP, see internal/middleware/ratelimit.go)
-- ARGV[1] = capacity (the bucket's maximum size / burst allowance)
-- ARGV[2] = refill rate, in tokens per second
-- ARGV[3] = cost of the request being made (always 1 from this codebase today)
-- ARGV[4] = "now", as a Unix timestamp with fractional seconds, supplied by the CALLER
--           (RateLimiter.Now, defaulting to time.Now) rather than read from Redis's own TIME
--           command. This was deliberately verified, not assumed: miniredis's FastForward (used
--           by ratelimit_test.go to advance time without a real sleep) advances its own internal
--           key-expiry clock, but NOT what its Lua environment's `redis.call('TIME')` returns --
--           confirmed empirically before writing this script this way. Taking `now` as an
--           explicit argument makes the refill math independent of Redis's own clock entirely
--           (real or simulated), which is what makes it possible to test deterministically at
--           all without a real sleep of the fixed elapsed duration.
--
-- Returns {allowed, tokens_remaining}, both as strings -- Lua numbers returned to a Redis client
-- get truncated to integers by the EVAL reply conversion, which would silently destroy the
-- fractional token count this script depends on (e.g. for computing Retry-After); returning
-- tostring(...) instead preserves full precision, parsed back to a float on the Go side.
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])

local bucket = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(bucket[1])
local last_ts = tonumber(bucket[2])

if tokens == nil then
    -- No bucket yet for this key: start full, as of now.
    tokens = capacity
    last_ts = now
end

local elapsed = now - last_ts
if elapsed < 0 then
    -- A caller-supplied `now` earlier than the bucket's own last_ts should not happen in
    -- practice, but treat it as "no time has passed" rather than let a negative elapsed value
    -- subtract tokens.
    elapsed = 0
end
tokens = tokens + (elapsed * refill_rate)
if tokens > capacity then
    tokens = capacity
end

local allowed = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
end

redis.call("HMSET", key, "tokens", tostring(tokens), "ts", tostring(now))
-- Let an idle client's key expire well after its bucket would have fully refilled on its own, so
-- Redis does not accumulate one key per IP forever.
local ttl = math.ceil(capacity / refill_rate) + 60
redis.call("EXPIRE", key, ttl)

return {tostring(allowed), tostring(tokens)}
