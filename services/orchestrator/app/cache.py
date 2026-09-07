"""Semantic response cache (Phase 8): serves a repeat question from a prior, fully-verified
answer instead of paying for another retrieval + generation + verification cycle.

Off by default (Settings.SEMANTIC_CACHE_ENABLED, read by app/pipeline.py) -- a fresh clone's
`docker compose up` and the CI invariant gate are byte-for-byte unaffected: nothing in this module
is ever called unless that setting is explicitly turned on.

WHAT gets cached: only a fully verified ANSWER or REFUSAL_ADVICE `AnswerResponse` (see
`CACHEABLE_RESPONSE_TYPES` below). A CLARIFY, NO_ANSWER, or BLOCKED_UNVERIFIED response is never
stored -- caching a CLARIFY would freeze one clarifying question onto a family of superficially
similar-sounding queries; caching a NO_ANSWER or BLOCKED_UNVERIFIED would freeze a failure to
answer, which a later re-index or a slightly reworded question deserves a fresh chance at, not a
served-from-cache repeat of a miss.

HOW similarity is measured: the SAME embedding the query path already computes for retrieval (see
app/pipeline.py -- this module never embeds anything itself), compared by pgvector cosine distance
against every cached entry's own question embedding, exactly the same distance metric
app/db.py::hybrid_search already uses. The threshold is
Settings.SEMANTIC_CACHE_SIMILARITY_THRESHOLD; see that setting's own comment in app/config.py for
the real measurements it was derived from
(paraphrases of real golden questions vs. the closest genuinely-different question pair in the same
golden set) and the honest gap in that derivation.

HOW invalidation works: every cache row is stamped with `corpus_version()` at write time, and
`lookup()` only ever searches rows matching the CURRENT corpus_version -- never across versions. A
version is `max(sources.last_changed_at)` (moved forward by every meaningful re-index --
app/recrawl.py::reindex_source always sets it) combined with `count(documents)` (a second,
independent signal against a future write path that changes chunk count without moving
last_changed_at). `store()` additionally DELETES every row whose corpus_version does not match the
version it is about to write under, so the table never accumulates rows from a superseded corpus
state at all -- a corpus change does not just make old entries unreachable, it wipes them outright.

WHY `lookup()` ALSO PARTITIONS ON THE ADVICE/INFORMATION CLASSIFICATION (Phase 8 round 4, fixing a
real bug a round-3 semantic-only cache had): a cosine-distance threshold measures TOPIC closeness,
not INTENT. "What is the rule on X" and "what should I do about X" are neighbours in embedding
space by construction -- an embedding encodes what a question is ABOUT far more strongly than
whether it is asking for a fact or asking for advice. Measured directly (real nomic-embed-text
embeddings, reported in docs/reports/phase-8.md): "Does my employer need E-Verify for the STEM
extension?" (informational) sits at cosine distance 0.0885 from "Should I switch to an E-Verify
employer to get the STEM extension?" (advice-seeking) -- deep inside the 0.15 threshold below, and
NOT a cherry-picked outlier: "Which form do I file for the OPT work permit?" vs. "Which form should
I file for my OPT work permit?" measured 0.0175, and two more of six realistic same-topic
factual/advice pairs measured landed inside 0.15 as well. Before this fix, `lookup()` selected the
nearest cache entry for a corpus version with NO regard for what kind of response was stored there,
so an advice-seeking query could be served a cached factual ANSWER verbatim (or vice versa) --
silently skipping the classification app/pipeline.py had ALREADY computed and, on that path,
discarding it. Reproduced end to end against the real pipeline before this fix: the question
"Should I switch to an E-Verify employer to get the STEM extension?", asked once with the cache
off, correctly returned response_type=refusal_advice with a DSO/attorney redirect; asked again with
only the cache turned on, it was served the OTHER question's cached response_type=answer, with no
refusal and no redirect at all. That is exactly the "an advice-seeking question tells the reader
what to do" failure ARCHITECTURE.md's product boundary forbids, produced by a feature that has
nothing to do with the classifier itself -- the classifier was never wrong; its answer was thrown
away.

NO THRESHOLD FIXES THIS. Lowering SEMANTIC_CACHE_SIMILARITY_THRESHOLD until these specific measured
pairs stop colliding would tune this project against the exact phrasings it happened to measure;
the next real user's phrasing sits just as close, for the same structural reason (topic and intent
are not the same axis, and no single number separates "about X" from "about X, but personal"). The
fix instead makes the classification part of what `lookup()` is allowed to match on: `lookup()`
takes `is_advice: bool` as a REQUIRED, keyword-only argument (no default -- there is no permissive
value for a future call site to fall back to) and only ever considers a cache row whose stored
`response_type` corresponds to that SAME classification (ANSWER for is_advice=False,
REFUSAL_ADVICE for is_advice=True -- see `_required_response_type` below). This does not need a new
column: app/pipeline.py already derives `response_type` from `classification.is_advice` with no
other path (`ResponseType.REFUSAL_ADVICE if classification.is_advice else ResponseType.ANSWER`), so
`response_type` already IS the classification, persisted on every row precisely because `store()`
already writes it. Filtering on it is filtering on the classification; there is nothing else to
add.

RESIDUAL RISK, STATED HONESTLY, NOT PAPERED OVER: this partition closes the cross-classification
failure above -- it does not, and cannot, close every semantic-cache failure mode. Two GENUINELY
DIFFERENT questions that share both a topic AND the same classification (for example two distinct
factual questions that both happen to be purely informational) can still land within
SEMANTIC_CACHE_SIMILARITY_THRESHOLD of each other and collide, and the wrong one's fully-verified,
real-looking citations would be served for the other. That risk is inherent to ANY similarity-based
cache and is not eliminated by this fix -- what this fix removes is specifically the risk of
crossing the advice/information safety boundary, which CLAUDE.md treats as a hard line, not a
quality nuance. See docs/adr/0010-cache-classification-partition.md for the full reasoning.

NO PII: no question text, no client identifier, and no session information is ever stored here --
only the query's embedding vector (not reversible to the literal question text) and the response
payload itself (which already carries no personal information; see ARCHITECTURE.md, "No personal
identifying information is stored").

OPTIONAL INFRASTRUCTURE, NEVER FATAL (Phase 8 round 5): `corpus_version`, `lookup`, and `store` all
catch ANY database error, log it at WARNING with the real exception, and degrade -- `corpus_version`
returns `None` (meaning "cache disabled for this request"), `lookup` returns `None` (a cache miss,
indistinguishable from a real one to its caller), and `store` simply does not persist the row. A
cache is optional by definition: this exact bug class (a live database that predates a newly-added
table -- here, `query_cache` -- turning a successful, already-generated answer into a 502 because an
unrelated write or read failed afterward) is what took down POST /query for `record_query` in
app/usage.py before that module's own Phase 8 round 5 fix; this module gets the identical treatment,
proactively, for the same reason: enabling `SEMANTIC_CACHE_ENABLED` must never make the answer path
LESS reliable than leaving it off. app/pipeline.py already treats a `None` corpus_version as "skip
the cache lookup, and skip the store at the end too" (both gated on `cache_version is not None`), so
a `corpus_version` failure alone is enough to cleanly opt this request out of caching without any
extra branching at the call site.

WHERE THE LINE IS: retrieval itself (app/db.py::hybrid_search, which reads `documents` -- a REQUIRED
table, unlike the three optional ones this module and app/usage.py touch) is never wrapped this way,
here or anywhere else. If `documents` is genuinely gone, there is nothing to answer with, cached or
otherwise, and that failure must keep propagating out of app/pipeline.py to app/main.py's existing
502 path -- see app/usage.py's own module docstring for the fuller statement of this boundary.
"""

from __future__ import annotations

import json
import logging

from pgvector import Vector
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.schemas import AnswerResponse, ResponseType

logger = logging.getLogger(__name__)

# Only these two response types are ever written to the cache -- see the module docstring for why
# CLARIFY/NO_ANSWER/BLOCKED_UNVERIFIED are excluded. store() enforces this itself (not merely by
# convention at each call site), so a future call site cannot silently widen what gets cached.
CACHEABLE_RESPONSE_TYPES = frozenset({ResponseType.ANSWER.value, ResponseType.REFUSAL_ADVICE.value})


async def corpus_version(pool: AsyncConnectionPool) -> str | None:
    """A string identifying "the corpus as it stands right now" -- see the module docstring for
    what it is built from and why. Never a version number this code invents or increments itself;
    always re-derived from `sources`/`documents` directly, so it is correct even if some other
    process (a manual SQL edit, a different service instance) changed the corpus.

    Returns `None` on ANY database error (logged at WARNING) instead of raising -- see the module
    docstring, "OPTIONAL INFRASTRUCTURE, NEVER FATAL". app/pipeline.py's caller already treats
    `None` as "cache disabled for this request" and skips both the lookup and the store; this
    function does not need to know that, it just has to fail into a value the caller already
    handles safely.
    """
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT (SELECT max(last_changed_at) FROM sources), "
                    "(SELECT count(*) FROM documents)"
                )
                max_last_changed_at, chunk_count = await cur.fetchone()
    except Exception:  # noqa: BLE001 - the cache is optional; degrade, never break /query
        logger.warning(
            "failed to compute corpus_version for the semantic cache; disabling the cache for "
            "this request",
            exc_info=True,
        )
        return None
    changed_at_part = max_last_changed_at.isoformat() if max_last_changed_at is not None else "none"
    return f"{changed_at_part}:{chunk_count}"


def _required_response_type(is_advice: bool) -> str:
    """The exact `response_type` a cache row must carry to ever be returned to a query classified
    `is_advice` -- see the module docstring's "WHY `lookup()` ALSO PARTITIONS..." section. There is
    deliberately no third option: `is_advice` is a bool, not a string, so a caller cannot pass
    anything that resolves to "match any classification".
    """
    return ResponseType.REFUSAL_ADVICE.value if is_advice else ResponseType.ANSWER.value


async def lookup(
    pool: AsyncConnectionPool,
    query_embedding: list[float],
    *,
    is_advice: bool,
    threshold: float,
    version: str,
) -> AnswerResponse | None:
    """Return the cached `AnswerResponse` if the nearest cache entry FOR THIS `version` AND FOR
    THIS `is_advice` classification is within `threshold` cosine distance of `query_embedding`;
    otherwise None (a cache miss). Never considers a row from any other corpus_version (see
    `store()` for why not) OR a row whose stored `response_type` does not correspond to
    `is_advice` (see the module docstring's "WHY `lookup()` ALSO PARTITIONS..." section) -- a cache
    row stored for the OTHER classification is invisible to this call no matter how close its
    embedding is.

    `is_advice` is REQUIRED and keyword-only, with no default: the caller (app/pipeline.py) already
    computed this classification before ever reaching this call, so there is no reasonable default
    to fall back to, and a future call site that forgets to pass it fails to import/call at all
    rather than silently reopening the cross-classification bug this signature exists to close.

    Returns `None` (a cache miss, indistinguishable from a real one) on ANY database error too --
    logged at WARNING -- rather than raising. See the module docstring, "OPTIONAL INFRASTRUCTURE,
    NEVER FATAL": if `query_cache` itself is missing or unreadable, the correct behavior is exactly
    what a normal cache miss already looks like -- fall through to a real retrieval and generation,
    not a 502 for a request the pipeline could otherwise have answered.
    """
    vector = Vector(query_embedding)
    required_response_type = _required_response_type(is_advice)
    try:
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT response_json, embedding <=> %(embedding)s AS distance
                    FROM query_cache
                    WHERE corpus_version = %(version)s
                      AND response_type = %(response_type)s
                    ORDER BY distance
                    LIMIT 1
                    """,
                    {
                        "embedding": vector,
                        "version": version,
                        "response_type": required_response_type,
                    },
                )
                row = await cur.fetchone()
    except Exception:  # noqa: BLE001 - the cache is optional; degrade to a miss, never raise
        logger.warning(
            "semantic cache lookup against query_cache failed; treating this as a cache miss",
            exc_info=True,
        )
        return None
    if row is None or row["distance"] > threshold:
        return None
    return AnswerResponse.model_validate(row["response_json"])


async def store(
    pool: AsyncConnectionPool,
    query_embedding: list[float],
    response: AnswerResponse,
    *,
    version: str,
) -> None:
    """Cache `response` under `query_embedding`, stamped with `version`. A no-op for any
    response_type other than ANSWER/REFUSAL_ADVICE (see module docstring) -- app/pipeline.py is
    only expected to call this on those two paths, but this function refuses anything else too, so
    a future call site cannot accidentally widen what gets cached just by calling it.

    Every existing row whose corpus_version does not match `version` is deleted first, in the same
    transaction as the insert: a corpus change wipes every superseded-version row outright rather
    than merely making it unreachable to `lookup()` -- see the module docstring's "HOW invalidation
    works".

    NEVER RAISES on a database error (Phase 8 round 5): by the time this is called, verification has
    already passed and a real, renderable response has been built (see app/pipeline.py -- this is
    the LAST thing that runs before returning it). Failing to persist that response into the cache
    is a lost optimization for the NEXT identical question, never a reason to fail THIS one. Logged
    at WARNING with the real exception, then simply returns.
    """
    if response.response_type not in CACHEABLE_RESPONSE_TYPES:
        return
    vector = Vector(query_embedding)
    payload = json.dumps(response.model_dump(mode="json"))
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM query_cache WHERE corpus_version != %s", (version,)
                    )
                    await cur.execute(
                        """
                        INSERT INTO query_cache
                            (embedding, corpus_version, response_type, response_json)
                        VALUES (%s, %s, %s, %s::jsonb)
                        """,
                        (vector, version, response.response_type, payload),
                    )
    except Exception:  # noqa: BLE001 - the cache is optional; a write failure must not fail /query
        logger.warning(
            "failed to write this response into query_cache; the response is still being served, "
            "it just will not be served from cache next time",
            exc_info=True,
        )
