"""Usage counters and the daily generation budget cap (Phase 8). Two related, Postgres-backed
concerns that live together because both are about "how much has this service done", not about
answering a question:

1. A persistent count of total queries handled and distinct anonymous sessions seen -- so a
   "handled N queries for N people" number is real and survives a restart (see `get_usage_counts`,
   exposed by GET /usage in app/main.py).
2. A per-UTC-day cap on generation calls -- so a runaway cost cannot exceed a known daily ceiling
   (see `get_generation_count_today`/`record_generation_call`, read by app/pipeline.py before it
   calls the generator).

NO PII, EVER: see `hash_session_identifier` below for the one identifier this module ever touches,
and why what gets stored can never be turned back into who asked.

OPTIONAL INFRASTRUCTURE, NEVER FATAL (Phase 8 round 5): every function in this module that writes
or reads one of `usage_totals`/`usage_sessions`/`daily_generation_counts` -- `record_query`,
`record_generation_call`, and `budget_exceeded` (via `get_generation_count_today`) -- catches ANY
database error, logs it (never silently), and degrades instead of raising. This is a real bug fix,
not a hypothetical: pointed at a live database that predated this project's newest tables (init.sql
applied, but before these three tables existed), the generator would SUCCEED -- a correct, grounded,
cited answer already sitting in memory -- and then `record_query`'s `UPDATE usage_totals ...` would
raise `psycopg.errors.UndefinedTable`, which app/main.py's blanket `except Exception` around the
whole /query handler turned into a 502, discarding the good answer along with the analytics write
that actually failed. A usage counter and a cost cap are analytics and cost control, not correctness
or safety: nothing about "was this answer grounded and cited" depends on whether a `total_queries`
counter incremented, so a failure recording it must never be the reason a real answer does not
reach the caller.

`get_usage_counts` (the read behind GET /usage, a reporting endpoint that is not part of answering
a query) is deliberately NOT wrapped here -- if `usage_totals`/`usage_sessions` are genuinely gone,
that endpoint failing loudly is the correct, informative behavior for whoever is checking it; there
is no "serve the answer" fallback for a request whose entire purpose is to report a count.

WHERE THE LINE IS (do not extend this pattern past this module): the advice classifier's decision,
citation verification, the no-answer gate, and retrieval itself (app/db.py::hybrid_search) are
SAFETY guardrails, not analytics, and none of them is ever wrapped this way anywhere in this
codebase. A failure in any of those must keep propagating out of app/pipeline.py exactly as it does
today, so app/main.py's existing 502 path is still the outcome for a REAL failure -- see
app/pipeline.py's own module docstring for why swallowing a guardrail failure would silently turn
"I could not verify this" into "here is an answer nobody checked."
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


def hash_session_identifier(raw_identifier: str, salt: str) -> str:
    """Turn a raw per-connection identifier (the caller's IP address, at every call site in this
    codebase -- see app/main.py) into an opaque, anonymous session hash: HMAC-SHA256 keyed on
    `salt` (Settings.SESSION_HASH_SALT, a server-side secret that never appears in a response, a
    log line, or this database), hex-encoded.

    WHY THIS CANNOT BE REVERSED TO IDENTIFY ANYONE: HMAC-SHA256 is a one-way function -- there is
    no computation that recovers `raw_identifier` from the hash alone, only brute force: trying
    every candidate input through HMAC with the same salt until one output matches. Two things make
    that brute force the wrong shape of attack to worry about here. First, the salt is a secret
    this process alone holds (an environment variable, never logged, never returned by any
    endpoint, never written into `query_cache` or anywhere else) -- without it, brute-forcing even
    a small candidate space (every IPv4 address is only ~4 billion) produces nothing verifiable,
    because computing HMAC(candidate, salt) requires already knowing the salt. Second, even
    someone who DID hold the salt (a database leak alone never exposes it, since it is never
    stored) would still need a candidate raw identifier to test the hash against -- the hash by
    itself narrows nothing down;
    it only lets you confirm "was this hash produced from THIS specific candidate", which requires
    already knowing (or already suspecting) that candidate. That is the deliberate, bounded claim:
    this scheme prevents a database leak from handing anyone a list of raw client identifiers, and
    prevents this service from ever persisting one in the first place -- it is not a defense against
    an attacker who already has both the raw identifier and the secret salt, a threat model with no
    stored secret can fully close.
    """
    return hmac.new(
        salt.encode("utf-8"), raw_identifier.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------------------------------------
# Persistent usage counters (item 4): total queries handled, distinct anonymous sessions seen.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageCounts:
    total_queries: int
    distinct_sessions: int


async def record_query(pool: AsyncConnectionPool, session_hash: str | None) -> None:
    """Record that one query was handled. Always increments `usage_totals.total_queries` by
    exactly one. When `session_hash` is given (non-empty), also records that this hash has been
    seen at least once, via an upsert that only ever touches `first_seen_at` on the FIRST time a
    given hash appears -- `usage_sessions` holds one row per DISTINCT hash, so
    `get_usage_counts`'s "distinct_sessions" is a plain `count(*)` over it, never a re-derived
    approximation.

    `session_hash=None` (no caller identifier was available at all, e.g. a test calling
    app/pipeline.py directly with no HTTP request in play) still increments total_queries -- a
    query was still genuinely handled -- but contributes nothing to distinct_sessions, which is
    the honest thing to do: recording a fabricated or placeholder hash would inflate the distinct
    count with sessions that were never real.

    NEVER RAISES (Phase 8 round 5): any database error here (most commonly `usage_totals`/
    `usage_sessions` not existing yet, or the connecting role lacking a grant on one of them) is
    caught, logged at WARNING with the real exception, and swallowed -- the caller (app/main.py's
    POST /query, always AFTER `answer_question` has already produced and verified a response) must
    still return that answer. See this module's own docstring, "OPTIONAL INFRASTRUCTURE, NEVER
    FATAL", for why this specific function is in scope for that and what stays out of scope.
    """
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE usage_totals SET total_queries = total_queries + 1 WHERE id = 1"
                    )
                    if session_hash:
                        await cur.execute(
                            """
                            INSERT INTO usage_sessions (session_hash)
                            VALUES (%s)
                            ON CONFLICT (session_hash) DO NOTHING
                            """,
                            (session_hash,),
                        )
    except Exception:  # noqa: BLE001 - analytics must never break a query that already succeeded
        logger.warning(
            "failed to record usage counters (usage_totals/usage_sessions) for this query; the "
            "query itself already succeeded and is still being served",
            exc_info=True,
        )


async def get_usage_counts(pool: AsyncConnectionPool) -> UsageCounts:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT total_queries FROM usage_totals WHERE id = 1")
            row = await cur.fetchone()
            total_queries = row[0] if row else 0
            await cur.execute("SELECT count(*) FROM usage_sessions")
            (distinct_sessions,) = await cur.fetchone()
    return UsageCounts(total_queries=total_queries, distinct_sessions=distinct_sessions)


# ---------------------------------------------------------------------------------------------
# Daily generation budget cap (item 3): a per-UTC-day count of generation calls, persisted so a
# restart never resets it mid-day.
# ---------------------------------------------------------------------------------------------


def _today_utc() -> datetime:
    return datetime.now(UTC)


async def get_generation_count_today(pool: AsyncConnectionPool) -> int:
    """How many generation calls have been recorded for the current UTC day so far. 0 for a day
    with no row yet -- there is nothing to count until the first call of that day is recorded.
    """
    today = _today_utc().date()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT generation_count FROM daily_generation_counts WHERE day = %s", (today,)
            )
            row = await cur.fetchone()
    return row[0] if row else 0


async def record_generation_call(pool: AsyncConnectionPool) -> None:
    """Record one successful generation call against today's UTC day, creating that day's row on
    its first call. Called by app/pipeline.py ONLY after `llm.generate()` actually succeeds -- a
    call that raised before producing anything never reaches here, so the count reflects
    generation calls that actually happened, not merely attempted.

    NEVER RAISES (Phase 8 round 5): by the time this is called, the generator has already produced
    an answer that will go on to be verified and rendered -- a failure to persist THIS COUNTER
    (`daily_generation_counts` missing, or a grant issue) must not turn that already-earned answer
    into a 502. Caught, logged at WARNING with the real exception, swallowed. The cost consequence
    of a swallowed write here is bounded and self-correcting: at worst, one call this UTC day goes
    uncounted, so the cap (if configured) is enforced against a count that is one lower than the
    true number of calls actually made -- never the other direction, and never a reason to fail
    a request that already succeeded. See this module's own docstring, "OPTIONAL INFRASTRUCTURE,
    NEVER FATAL".
    """
    today = _today_utc().date()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO daily_generation_counts (day, generation_count)
                    VALUES (%s, 1)
                    ON CONFLICT (day) DO UPDATE SET
                        generation_count = daily_generation_counts.generation_count + 1
                    """,
                    (today,),
                )
    except Exception:  # noqa: BLE001 - a cost counter must never break an answer already produced
        logger.warning(
            "failed to record a generation call in daily_generation_counts; today's persisted "
            "count did not increment for this call, but the answer that already succeeded is "
            "unaffected",
            exc_info=True,
        )


async def budget_exceeded(pool: AsyncConnectionPool, cap: int) -> bool:
    """True if today's UTC generation count has already reached `cap`. `cap <= 0` means "no cap
    configured" (Settings.DAILY_GENERATION_CAP's default) and always returns False without a
    database round trip -- a fresh clone and the CI invariant gate never pay for this check at all
    unless an operator has actually turned the cap on.

    FAILS OPEN ON A DATABASE ERROR (Phase 8 round 5), DELIBERATELY, NOT BY DEFAULT SAFETY: if
    `get_generation_count_today` cannot even be answered (`daily_generation_counts` missing, or a
    grant issue), this returns False -- "not exceeded" -- rather than letting that exception
    propagate and turn an unrelated analytics-table outage into every single query failing. This is
    logged at ERROR (louder than a plain WARNING), because failing open silently defeats the whole
    point of a cost cap someone deliberately turned on, and an operator needs to know the cap
    stopped being enforced, not just that "something happened."
    REASONING FOR FAIL-OPEN OVER FAIL-CLOSED, STATED EXPLICITLY: the cap
    (Settings.DAILY_GENERATION_CAP) is a SOFT cost control an operator opts into -- it exists to
    stop a runaway bill, not to stop an unsafe answer from rendering (that job belongs to the
    guardrails this module's own docstring names, none of which fail open). Failing closed here
    would mean a single broken analytics table takes the entire answer path down for every caller,
    which is a strictly worse outcome than the cost risk of serving some number of queries without
    the cap enforced for as long as the table stays broken -- a bounded, recoverable cost exposure,
    not a safety violation. If this reasoning changes (e.g., the cap is ever asked to double as a
    hard safety limit rather than a cost control), fail-closed would be the right call instead; it
    is not what this cap is for today.
    """
    if cap <= 0:
        return False
    try:
        count = await get_generation_count_today(pool)
    except Exception:  # noqa: BLE001 - see this function's own docstring for the fail-open choice
        logger.error(
            "failed to read today's generation count from daily_generation_counts; FAILING OPEN "
            "(the daily generation cap is NOT being enforced for this request) rather than "
            "turning this analytics/cost-control table's outage into a hard failure of the "
            "answer path",
            exc_info=True,
        )
        return False
    return count >= cap
