# Phase 8 Verification Report

Status: BLOCKED

Loop summary: 8 rounds. Round 1 built the hosted provider chain, the family guard, and the deploy
config; I sent it back with six defects (a test that broke the CI gate by reading the ambient
environment, an `llm.served_by` log line that could never be seen under uvicorn, a family guard whose
production case asserted a literal against itself, scale-to-zero on both Fly machines, a `vercel.json`
at a path Vercel never reads, and a production image carrying the whole RAGAS stack). Round 2 fixed
all six and closed the freshness loop. Round 3 added the cost controls, usage counter and dashboard.
Round 4 fixed the most serious defect of the phase, a semantic cache that bypassed the advice
guardrail, plus the gpt-oss citation-format mismatch and the Grafana Cloud and Langfuse wiring.
Round 5 fixed an analytics write that could 502 the entire answer path. Rounds 6 and 7 wrote and then
corrected the README. Round 8 settled the production embedding provider, which had been the largest
open item. No check was weakened at any point.

Two defects were found by running things rather than reading them, and neither would have surfaced
from the builder's own summaries. Both are written up below.

## Machine-checkable gate

- [x] **1. The eval runs against the hosted provider, generating on Ollama Cloud `gpt-oss:120b`** —
  ran: `python -m eval.run` against a live orchestrator on the 221-chunk corpus — got: 21/21 rows
  scored, 0 errored, 0 empty answers, judge served `nvidia/nemotron-3.5-lightning-30b-a3b` as
  requested, determinism check identical across two runs. Result written to
  `eval/results/20260907T015332Z.json`.

  **5 of 7 gated thresholds pass, against 2 of 7 on the Phase 4 baseline** (`20260905T234405Z`):

      metric                       threshold   hosted            P4 baseline
      faithfulness                 >= 0.85     PASS  0.875       FAIL  0.841
      answer_relevancy             >= 0.75     FAIL  0.641       FAIL  0.600
      context_precision            >= 0.70     PASS  0.905       PASS  0.937
      false_refusal_rate           <= 0.10     PASS  0.067       FAIL  0.400
      advice_leakage_rate          <= 0.10     PASS  0.000       FAIL  0.167
      comprehensibility            >= 3.5      FAIL  3.333       FAIL  3.190
      citation_hallucination_rate  == 0.0      PASS  0.000       PASS  0.000

  `overall_pass` is False on both runs. The thresholds are aspirational and no run in this project
  has ever met them, by design (`docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md`), so this
  is not a Phase 8 regression. Three metrics moved from fail to pass: faithfulness, false refusal,
  and advice leakage.

  **Did the provider swap move answer quality? Yes, slightly upward, but only after a parsing defect
  was fixed.** The first hosted run scored faithfulness 0.646, well below the baseline's 0.841, and
  returned `blocked_unverified` for 5 of 21 rows. The cause was not answer quality. See item 1a.

- [x] **1a. The gpt-oss citation-format defect** — `gpt-oss:120b` cites in its own training markup,
  `【1†L2-L5】`, rather than this project's `[1]` convention, so `parse_cited_indices` saw an entirely
  uncited answer and `verify_citations` correctly blocked a correct, grounded answer. Measured over
  15 real generations on the 5 affected questions: **9 used `[n]`, 6 used the native markup, 0
  produced no citation at all**. The model grounds every answer; the project could not read 40% of
  its citations.

  Response types before and after the fix:

      P4 baseline (qwen3.5-8k)     answer 15,  refusal_advice 6
      hosted, before the fix       answer  8,  refusal_advice 8,  blocked_unverified 5
      hosted, after the fix        answer 13,  refusal_advice 8,  blocked_unverified 0

  The verification itself was not loosened, and I checked that directly rather than taking it on
  trust: text with no citation still blocks with `answer_missing_citation`, and an out-of-range
  native-format index still blocks with `citation_index_out_of_range`. The normalizer cannot
  manufacture a citation where the model produced none.

- [x] **2. Failover is proven, not configured** — ran: a real `POST /query` against a live
  orchestrator whose primary generator was pointed at a model that does not exist, with NVIDIA as the
  configured fallback — got:

      INFO app.providers.llm llm.primary_failed provider=ollama error=Ollama /api/chat returned
           HTTP 404 for model 'gpt-oss:120b-PRIMARY-DELIBERATELY-BROKEN'
      INFO app.providers.llm llm.served_by provider=nvidia model=openai/gpt-oss-20b attempt=2
      INFO:     "POST /query HTTP/1.1" 200 OK

  The response carried 5 real citations. The request succeeded on the fallback, and the log names
  which provider served it.

- [x] **3. The generator and judge never resolve to the same family** — families in play: primary
  `gpt-oss:120b` and fallback `openai/gpt-oss-20b` are both `gpt-oss`; judge
  `nvidia/nemotron-3.5-lightning-30b-a3b` is `nemotron`. Both generator paths clear the judge.

  The guard is enforcement, not just a test. `eval/run.py` calls
  `assert_no_generator_judge_family_collision` before any judge or generation call. Ran it with a
  deliberately colliding fallback (`nvidia/nemotron-3-nano`) and the orchestrator pointed at a dead
  port - got exit code 1 and:

      ModelFamilyCollisionError: Judge 'nvidia/nemotron-3.5-lightning-30b-a3b' (family 'nemotron')
      shares a model family with 1 generator(s) in the resolved failover chain:
      'nvidia/nemotron-3-nano' (family 'nemotron').

  The dead port proves ordering: the colliding config never reaches a connection error, while a
  non-colliding control config with the same dead port proceeds to `ConnectError` on every row. An
  unclassifiable model id raises rather than passing quietly.

- [~] **4. Ollama Cloud quota consumed by a full eval run** — measured from the orchestrator's own
  logs: **29 generation calls for a 21-row run** (21 answer generations plus 8 Layer 2 classifier
  escalations), 0 failovers, 0 rate-limit or quota errors. Token counts recorded by the provider for
  that run: prompt 147,254, completion 11,425.

  Expressing that as a percentage of the session and weekly limits is a HUMAN-REVIEW item: Ollama
  Cloud does not expose its free-tier limits through the API, so the denominator is only visible on
  your account page.

  Worth recording separately: a further **47 calls were burned** by the defect in item 9a, generating
  answers that were then discarded by a failed counter write. That is the concrete price of that bug.

- [x] **4a. Production embeds in-process from Ollama's own GGUF, in the same vector space** — the
  221 stored vectors were built by local Ollama and neither hosted provider serves
  `nomic-embed-text`, so the orchestrator now loads that same GGUF through llama-cpp-python. Against
  25 real stored vectors, minimum cosine **0.99999412**. Stronger than the cosine, 5 of 5 real
  queries retrieve the identical chunk set in the identical order under both embedders, and the
  off-topic control still returns `no_answer`, so `NO_ANSWER_MAX_DISTANCE=0.50` behaves identically
  and never needed re-deriving.

  The guard that makes this safe: llama.cpp's default `n_batch=512` silently splits longer inputs
  and returned different vectors for 5 of those 25 chunks (min 0.974, all of them at least 583
  tokens). `n_batch` and `n_ubatch` derive from one `n_ctx` setting, and the embedder raises rather
  than truncating. I proved the test catches a regression by forcing `n_batch=512` back in and
  watching it fail, then restoring it.

  Cost: orchestrator 512MB to 1GB, stack $5.13 to $7.64 a month at `iad`, against $10.83 for a
  separate embedding machine. Serving peaks at 358MB and embeds a query in 16ms on one thread. The
  production image grows 324MB to 1.06GB.

- [ ] **5. The corpus is MIGRATED to Neon, not re-ingested** — BLOCKED: no Neon connection string
  exists in `.env`. The pre-migration baseline is captured and ready to compare against:

      chunks 221 | sources 14 | sum(consecutive_failures) 0 | all 14 status 'ok'
      fetched_at        min 2026-08-29 02:01:32.865828+00  max 2026-09-06 03:25:03.095606+00
      last_verified_at  min 2026-09-06 03:25:37.253785+00  max 2026-09-06 03:26:45.145169+00
      last_changed_at   min 2026-08-29 02:01:32.865828+00  max 2026-09-06 03:25:03.095606+00
      embedding fingerprint  559334d566282225a2a4bc330192497f

  I confirmed `infra/sql/init.sql` is safe to apply to a populated database, which the migration
  depends on: applying it to a copy of the real corpus left the embedding fingerprint at
  `a52db4807ccc49dda2139aa45c541555` before and after, and applying it to the live database left all
  three fingerprints identical while creating the four new tables.

- [ ] **6. Which Neon connection string was used where** — BLOCKED for the same reason. The rule is
  documented in the README deploy guide (direct for `init.sql` and any DDL, pooled for the running
  application's `DATABASE_URL`) but has not been exercised against a real Neon project.

- [ ] **7. The rate limiter works against Upstash over TLS** — BLOCKED: no Upstash URL exists. What
  is verified is the mechanism, not the deployment: go-redis v9.18's `ParseURL` sets
  `TLSConfig{ServerName, MinVersion: TLS12}` for a `rediss://` URL and leaves it nil for `redis://`,
  confirmed by reading the module source, with a Go test asserting both. Real 429s from a deployed
  gateway against Upstash still need the credential.

- [x] **8. The cost controls work** — semantic cache: a repeated question is served from cache with
  0 generation calls, and the cache holds `answer` and `refusal_advice` in separate partitions.
  Budget cap with `DAILY_GENERATION_CAP=2`:

      call 1 -> type=answer      reason=None                          citations=5
      call 2 -> type=answer      reason=None                          citations=5
      call 3 -> type=no_answer   reason=daily_generation_cap_reached   citations=5

  The degraded response still carries the real retrieved sources and never fabricates an answer.
  HTTP 200 throughout, no 500s.

- [x] **9. A persistent usage counter survives a restart** — ran: `GET /usage`, `docker restart`, then
  `GET /usage` again, then one more query — got: `total_queries 3`, then `total_queries 3` after a
  fresh process, then `total_queries 4`. The count carried over.

- [x] **9a. Optional infrastructure can no longer take down the answer path** — this was the second
  defect I found by running rather than reading. Every query against the real database was returning
  502:

      INFO app.providers.llm llm.served_by provider=ollama_cloud model=gpt-oss:120b-cloud attempt=1
      ERROR app.main POST /query failed for question='How often do I report to my DSO...'
      psycopg.errors.UndefinedTable: relation "usage_totals" does not exist
      INFO:     "POST /query HTTP/1.1" 502 Bad Gateway

  Read the order. The generator succeeded and a write to an analytics counter threw the answer away.
  The live database simply predated the new tables, which is exactly the state a first deploy is in.

  After the fix, dropping each optional table in turn against a real-corpus database:

      dropped usage_totals             -> HTTP 200, response_type=answer, 5 real .gov citations
      dropped query_cache              -> HTTP 200
      dropped daily_generation_counts  -> HTTP 200
      dropped usage_sessions           -> HTTP 200
      dropped documents (REQUIRED)     -> HTTP 502, relation "documents" does not exist

  The boundary is right: optional infrastructure degrades with a logged warning, while retrieval and
  the safety guardrails still fail loudly.

- [~] **10. The dashboard renders its panels against real data** — all four panels return real series,
  queried through Grafana's own API against the local `grafana/otel-lgtm` container:

      office_hours_queries_total            answer 13, refusal_advice 8  (matches the eval run exactly)
      office_hours_generation_tokens_total  prompt 147254, completion 11425
      office_hours_generation_calls_total   41
      office_hours_eval_faithfulness        0.875297619047619  mode=full

  This is my laptop, not production, and that makes the item weaker than it sounds. See the
  human-review section.

  One real gap found here: the faithfulness panel was empty at first because `emit_run_metrics` reads
  `OTEL_EXPORTER_OTLP_ENDPOINT` from the eval process's own environment, and neither the README nor
  `docker-compose.yml` sets it for the eval path. The export failure is swallowed by design, so the
  panel would silently stay blank in normal use. I proved the mechanism by emitting from the stored
  run.

- [x] **11. A meaningful source change reports which golden rows are affected** — the lookup
  reproduces the ground truth I computed independently from `eval/golden.jsonl`, for all six cited
  sources and for the eight cited by nothing:

      stem-opt                             -> rows [0, 1, 4, 11, 12, 18, 19, 20]
      opt-for-f-1-students                 -> rows [2, 3, 9, 10, 18]
      h-1b-electronic-registration-process -> rows [5, 6, 14, 15]
      h-1b-cap-season                      -> rows [7, 13, 16]
      h-1b-specialty-occupations           -> rows [8, 17]
      f-1-optional-practical-training-opt  -> rows [19]
      an uncited source                    -> []   (empty, not a crash, not a false positive)

  It matches on `resolved_url` as well as `source_url`, which matters because one manifest entry
  redirects: a golden row citing only the post-redirect form still resolves, verified directly (a
  `resolved_url`-only match returns `[8, 17]`).

- [x] **12. The notification proposal was written, you chose, and only then was it built** — you chose
  red only when golden rows are affected. Exit codes verified across all seven cases:

      unchanged / cosmetic / robots_disallowed        -> 0
      meaningful, source cited by NO golden row       -> 0
      meaningful, source cited by golden rows         -> 1
      meaningful, golden set unreadable               -> 1   (fail-safe)
      fetch_failed                                    -> 1   (unchanged from before)

  A red run states which of the two meanings it carries at the top of the summary, and both together
  when both apply. `docs/adr/0009-golden-impact-gates-recrawl-red.md`.

- [x] **13. The deploy config exists and is valid** — both fly configs parse as TOML,
  `primary_region = "iad"` on both, internal ports match their services (8000 and 8080), health
  checks hit the real `/health` endpoints, `min_machines_running = 1` on both, and the OTLP endpoint
  and protocol are set. No credential is hardcoded in any committed config; a scan across `infra/`,
  `docker-compose.yml` and `.env.example` returned only a doc comment and a placeholder.
  `vercel.json` was moved to `services/frontend/`, because Vercel reads it from the repository root
  or the configured Root Directory and never from `infra/deploy/`. That deviates from CLAUDE.md's
  layout diagram, deliberately, because the layout's location cannot work.

- [x] **14. The README is complete** — 319 lines: architecture diagram, the real eval numbers with
  both regressions reported straight, the decisions and why with links to nine ADRs, local run steps,
  and a deploy guide in order with the exact `fly secrets set` lines and a table of every required
  environment variable. Checked: 0 em dashes, 0 terms from the banned promotional list, 0 formulaic
  contrasts, and every file path it names resolves on disk.

  Two factual errors were caught by checking rather than reading. `ollama pull qwen3.5-8k` would fail
  for every reader, because that model is not in the Ollama library (that library URL returns 404)
  and is built locally from the repository's own `Modelfile`. And three file paths were written from
  inside `services/orchestrator/` rather than from the repository root.

- [x] **15. ci-invariant-gate still passes** — ran the workflow's exact steps in `python:3.12-slim`
  with `[dev,eval-ci]` only, against a fresh database, ingesting only the fixture corpus:

      EXIT_init=0  EXIT_ingest=0  EXIT_health=0   (TOTAL: 17 chunks across 4 sources)
      python -m eval.run --ci        ->  OVERALL: PASS,  EXIT_eval_run_ci=0
      pytest -m "not full_corpus" -q ->  265 passed, 14 skipped, 11 deselected, 0 failed

  **Pre-work baseline, from `docs/reports/phase-7.md`: 130 passed, 9 skipped, 11 deselected, PASS.**
  Net +135 passing, +5 skipped, deselected unchanged, zero failures. The 5 new skips are two
  langgraph-marked tests and the three GGUF embedder tests, whose extras CI deliberately does not
  install. I ran this gate after every
  round and it went red exactly once, in round 1, from a test that read the ambient environment.

## Human-review items

- [ ] **Create the accounts and set the secrets.** Five credentials are missing and they are what
  blocks items 5, 6, 7 and the production half of 10: a Neon direct and pooled connection string, an
  Upstash `rediss://` URL, `OLLAMA_API_KEY`, and the Grafana Cloud and Langfuse values. `NOMIC_API_KEY`
  is no longer needed: the embedding provider is decided and needs no third-party key.
- [ ] **Push, and let the real workflow run.** All GitHub work is yours. I ran the workflow's steps
  locally in a dependency-matched container; the run itself is yours to trigger.
- [ ] **Check `git diff` on `eval/judge.py` and `eval/metrics.py`.** A misconfigured `black` run in
  round 2 reformatted 17 files. I recovered a frozen pre-Phase-8 copy of `app/` and `tests/` from a
  still-running container image and proved all 15 files there had identical ASTs, then restored the 6
  that differed in bytes to their committed form. Those two `eval/` files have no frozen baseline, so
  they are the only ones I could not check this way. The eval run exercises both end to end, so they
  work; the question is only whether the diff is pure reformatting.
- [ ] **Confirm the live URL answers a real question end to end through the gateway,** once deployed.
- [ ] **Set Vercel's Root Directory to `services/frontend`.** A dashboard setting no file can express.

## Quality metrics (reported, never optimized against)

From `eval/results/20260907T015332Z.json`, hosted `gpt-oss:120b`, 21/21 rows scored:

    faithfulness 0.875    answer_relevancy 0.641   context_precision 0.905
    false_refusal 0.067   advice_leakage 0.000     comprehensibility 3.333
    citation_hallucination 0.000   unreferenced_citation 0.686   reading_grade 15.128
    median /query latency 1.93s    (Phase 4 baseline: 7.07s)

Two moved the wrong way against the baseline. `context_precision` fell 0.937 to 0.905; retrieval is
unchanged between the runs, so this is mostly judge variance. `unreferenced_citation_rate` rose 0.486
to 0.686, and that one is real: the hosted run referenced 33 of 105 returned citations against the
baseline's 54, because `gpt-oss` writes shorter answers citing fewer passages.

`citation_hallucination_rate` reads 0.000 on both runs. That is the guardrail's boundary holding, not
a model achievement.

## How the core piece works

The semantic cache looked correct and was not. It found the nearest previously answered question by
cosine distance and, if that distance was under a threshold, returned the stored answer. The threshold
had been derived carefully from real measurements, and the closest different-question pair in the
golden set sat at 0.1595, comfortably outside the 0.15 threshold. The flaw is that the golden set's 21
questions are deliberately distinct from one another, so measuring only against them says nothing
about what real traffic looks like. I measured six realistic pairs where one person asks what a rule
says and another asks what they should do about that same rule, and two landed at 0.0885 and 0.0175,
deep inside the threshold. The reason is structural: an embedding encodes what a question is about far
more strongly than whether it wants a fact or a decision, so "does my employer need E-Verify" and
"should I switch to an E-Verify employer" are near neighbours by construction. That made the cache a
bypass of the one guardrail this project cannot get wrong. I reproduced it end to end: the same advice
question returned a refusal with the cache off, and a bare factual answer with no attorney redirect
and zero model calls with the cache on. No threshold fixes that, because tuning the number against the
pairs I happened to test just moves the collision to the next phrasing. The fix was to stop asking
distance to preserve something it never encoded, and make the guardrail's own decision part of the
cache key, so an advice question can only ever match an advice-shaped entry.

## Decisions logged

- `docs/adr/0009-golden-impact-gates-recrawl-red.md` - red only when a meaningful change touches a
  source that golden rows cite, and why "any meaningful change goes red" was rejected.
- `docs/adr/0010-cache-classification-partition.md` - a similarity threshold cannot separate intent
  from topic, so the guardrail decision belongs in the cache key.
- `docs/adr/0011-classifier-routing-excluded-from-family-guard.md` - why the Layer 2 classifier model
  sits deliberately outside the generator/judge family check.
- `docs/adr/0012-citation-markup-normalization.md` - a citation convention is part of the prompt
  contract, and a model swap can break it in a way that looks like a quality regression.
- `docs/adr/0013-in-process-gguf-embeddings.md` - run the identical GGUF in-process rather than
  re-embedding onto a hosted model, and why identical weights alone were not sufficient.

## Open items, named so they are findable later

- **Task-instruction prefixes for nomic-embed are unused.** Recorded in full at ARCHITECTURE.md,
  "Open experiments". Ollama applies no `search_query:` or `search_document:` prefix (its template
  is a bare `{{ .Prompt }}`, and raw text matches Ollama's own vectors at cosine 0.9999998 while the
  prefixed variants sit at 0.970 and 0.909). So all 221 stored vectors and every query omit the
  prefixes the model was trained with. Corpus and query are consistent, so this is a measurable
  experiment rather than a defect: retrieval quality is likely below what the model can deliver. The
  fix is a re-embed plus a re-derivation of `NO_ANSWER_MAX_DISTANCE`, and the re-derivation is the
  risky half, because 0.50 sits only 0.063 above the nearest off-topic control. ARCHITECTURE.md
  states the method and the acceptance criterion.

## Caveats and what is not done

The production embedding provider is decided and built: the orchestrator loads Ollama's own
`nomic-embed-text` GGUF in-process through llama-cpp-python. No embedding API, no second vendor, no
third machine, and no re-embed, so `NO_ANSWER_MAX_DISTANCE` stays 0.50 against the same space it was
derived in. `NOMIC_API_KEY` is no longer needed and neither is the Atlas equivalence test.
`docs/adr/0013-in-process-gguf-embeddings.md`.

Verified rather than assumed, because the obvious reasoning was not sufficient. Identical weights in
an identical engine still produced different vectors: llama.cpp's default `n_batch=512` silently
splits longer inputs, and against 25 real stored vectors it gave 5 below cosine 0.9999 (min 0.974),
every one a chunk of at least 583 tokens. With `n_batch = n_ubatch = n_ctx`, 0 of 25 fall below
0.9999, minimum 0.99999412. The end-to-end check is stronger than the cosine: 5 of 5 real queries
retrieve the identical chunk set in the identical order under both embedders, and the off-topic
control still returns `no_answer`, so the threshold behaves identically too.

Cost, measured against Fly's `iad` prices: the orchestrator goes from 512MB to 1GB, taking the stack
from $5.13 to $7.64 a month, against $10.83 for a separate embedding machine. Serving needs 358MB
peak at `n_ctx=2048` and embeds a query in 16ms on one thread. The production image grows from
324MB to 1.06GB, dominated by the 274MB GGUF layer and a 269MB dependency layer where numpy and its
native libraries account for roughly 107MB. That is a slower deploy, not a running cost.

Recording your point, because it is the honest lesson: neither of us caught the embedding gap when
the stack was decided. We specified the generator swap carefully and never asked what embeds the
query in production. The general form is that when you swap one provider, enumerate every model the
system calls, not just the one under discussion.

Grafana Cloud and Langfuse are wired into both services but have never posted to a real endpoint. The
Langfuse SDK call shape in particular is unit tested against a mock and needs a smoke test with real
keys. The Langfuse dependency added 14MB to the production image, which is now 338MB against 324MB.

A residual risk you should know about, pre-existing and not introduced this phase.
`app/guardrails/classifier.py` catches a Layer 2 model failure and returns `is_advice=False`, so it
fails open toward answering. Under stub providers, where Layer 2 never runs, measured advice leakage
is 0.500. A classifier outage therefore degrades advice detection to the Layer 1 rule silently, and
nothing surfaces it. It is deliberate, documented, and backstopped by prompt rule 5, and I did not
change it because Phase 8 is not its phase. The cheap improvement is a metric on
`decided_by="model_unavailable"` so the degradation is visible.

Two factual rows are still classified as advice by the hosted generator that were not by the local one
(`false_refusal_rate_structured` 0.133 against 0.000), both of the form "if I used X months, how much
do I have left". `gpt-oss`'s Layer 2 classifier is more cautious near the boundary than `qwen3.5`'s
was.

The `Modelfile` at the repository root is load-bearing for local setup and is absent from CLAUDE.md's
repository layout diagram. I do not edit CLAUDE.md; flagging it for you.
