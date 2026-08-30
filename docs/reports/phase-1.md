## Phase 1 Verification Report

Status: COMPLETE. All seven Definition-of-Done items are met. The eval harness scores any version of the running service from one command and exits non-zero on failure. Run `20260830T183110Z` scored **21 of 21 rows with zero errors** and reports `OVERALL: FAIL` because gated quality metrics genuinely fail. That is the correct Phase 1 outcome: the harness works, and the system it measures is not good enough yet.

Loop summary: 5 rounds, plus a process failure of mine that cost two runs.
- Round 1: harness crashed at row 4 of 21 with a 502 and had never written a results file. Added per-row error isolation, made the Ollama timeout configurable, captured latency, and stopped `main.py` and `run.py` from discarding the real error message.
- Round 2: first complete run. It reported `errored_rows: 0` and was wrong. 5 of 21 rows returned an empty answer and were scored as if real, and RAGAS died on a 429 taking all three of its metrics with it.
- Round 3: root-caused the empty answers to `llm.py` accepting `""` because it only raised on `None`. Made empty answers count as errored rows, stopped the judge scoring empty text, and isolated RAGAS failures per (row, metric).
- Round 4: identified from the captured payload that `qwen3.5-8k` is a reasoning model spending its whole output budget inside a `thinking` block. Disabled thinking on the generator. Empty answers went to zero.
- Round 5: three rows were still lost to judge 429s, so the phase was reported BLOCKED. Dropped the shared limiter from 35 to 20 requests/minute, raised the retry budget from 6 to 12 with backoff that keeps growing across all 12 attempts, fixed the RAGAS row attribution, and corrected the `LLM_MODEL` default. The next run scored 21 of 21.
- My failure: I left the first builder subagent live while starting a second. They ran concurrent eval runs against one Ollama instance and edited the same files, corrupting the timings in two runs. The user caught it from `docker top` and `ollama ps`. The process guard I now run before every eval is the fix, and it later caught a fourth unauthorized run before it could interfere.

### Machine-checkable gate (ALL green for COMPLETE)

- [x] **DoD 1. `python -m eval.run` prints per-metric scores and an overall PASS or FAIL** — ran: `docker compose exec -T orchestrator python -m eval.run` — got the table below, ending `OVERALL: FAIL`.

- [x] **DoD 2. Exit code non-zero on fail** — ran: `echo "EXITCODE=$?"` — got: `EXITCODE=1`. The PASS path returning 0 remains untested because no run has passed; the user plans to test it by tightening a threshold, which is the right way to check it.

- [x] **DoD 3. Every run writes a results file** — ran: `ls -la eval/results/` — got four files across four runs, latest `20260830T183110Z.json` (43 KB). Written by the container through the `./eval` bind mount and read on the host, which also proves DoD 7's mount.

- [x] **DoD 4. The judge is the NVIDIA endpoint, not the local model** — the results file records `judge_model_served: nvidia/nemotron-3.5-lightning-30b-a3b`, taken from the API's own response rather than from config. I probed the endpoint independently before the build: `chat_template_kwargs` sits at the request-body root, and with thinking disabled the response carries `reasoning_content: None` and a 13-token completion. `eval/judge.py` raises immediately if `JUDGE_PROVIDER` is not `nvidia` or the key is empty, and has no path that substitutes the local generator.

- [x] **DoD 4b. Judge stability** — got: `{'row_index': 0, 'score_1': 1, 'score_2': 1}`. Identical. This check originally ran on row 0 while row 0 was an empty answer, proving determinism only on empty input; it now selects the first row with a real answer.

- [x] **DoD 5. All 21 rows load and score, with subset breakdowns** — got: `scored 21/21, errored 0, empty 0, incomplete False`. Breakdowns present for `volatility`, `is_advice` and `multi_part`, each carrying its denominator. This was the item that blocked the phase for a round.

- [x] **DoD 6. `eval/README.md` documents the run command** — documents both the in-container and host forms, the previously undocumented seventh field `multi_part`, every threshold, which are gated versus reported, and the configurable rate limit.

- [x] **DoD 7. Compose passes the four `JUDGE_*` variables and mounts `./eval`** — ran: `docker compose config` — got `JUDGE_PROVIDER`, `JUDGE_BASE_URL`, `JUDGE_MODEL` and `JUDGE_API_KEY` on the orchestrator. Mount proven by the container writing results where the host reads them.

- [x] **Phase 0 regression gate** — `docker compose exec -T orchestrator pytest -v`: `10 passed`. `test_query.py` had gone red with a 502 from the thinking runaway and is green again.

- [x] **Lint** — `ruff check . && black --check .` inside the container: `All checks passed!`, `18 files would be left unchanged.`

- [x] **Anti-gaming** — `eval/golden.jsonl` unchanged throughout: 21 rows, 15/6 advice, 20/1 volatility, 18/3 multi_part, zero placeholders. All seven thresholds are the fixed originals. No test skipped or xfailed. Every change this phase was to transport (rate limits, retries, timeouts) or to surfacing errors, never to scoring.

### Quality metrics (reported, NOT gated, never optimized against)

Run `20260830T183110Z`, 21 of 21 rows scored.

```
metric                          value    n/total    threshold      gate   result
faithfulness                    0.910     20/21      >= 0.85      GATED    PASS
answer_relevancy                0.529     20/21      >= 0.75      GATED    FAIL
context_precision               0.914     21/21      >= 0.70      GATED    PASS
false_refusal_rate              0.200     15/15      <= 0.10      GATED    FAIL
advice_leakage_rate             0.833       6/6      <= 0.10      GATED    FAIL
comprehensibility               2.905     21/21      >= 3.5       GATED    FAIL
citation_hallucination_rate     0.000     21/21      == 0.0       GATED    PASS
unreferenced_citation_rate      0.733   105/105          n/a   REPORTED      --
reading_grade_level            15.638     21/21          n/a   REPORTED      --
```

Two rows are missing from a RAGAS metric each (hence 20/21 on faithfulness and answer_relevancy): row 13 lost `answer_relevancy` to a 502 from the endpoint, and row 16 lost `faithfulness` to the parse failure described below. Both are recorded by row with their real index and question.

By subset:

```
is_advice     n/total   faithfulness  ans_relevancy  ctx_precision  comprehens.  reading_grade
false          15/15          0.912          0.594          0.896        2.933         15.153
true             6/6          0.905          0.377          0.959        2.833         16.851

multi_part    n/total
false          18/18          0.894          0.526          0.900        2.944         15.759
true             3/3          1.000          0.542          1.000        2.667         14.912

volatility    n/total
stable         20/20          0.905          0.513          0.910        2.900         15.753
volatile         1/1          1.000          0.830          1.000        3.000         13.331
```

`advice_leakage_rate = 0.833` is the Phase 4 baseline: rows 3, 5, 6, 7 and 8 were answered instead of refused, and only row 4 refused. Expected with no refusal path, and untouched.

### What the `think:false` change did

Disabling the reasoning model's thinking block was the highest-value change in this phase.

```
                    before          after
empty answers       5 of 21         0 of 21
/query min             34.3s          2.16s
/query median         143.8s          5.98s
/query p95            182.5s         17.77s
/query max            215.6s         26.04s
pytest              9/10 (502)      10/10
```

A 24x drop in median query latency. The thinking tokens were essentially the entire cost, and they were also destroying roughly a quarter of the answers outright. Wall clock stayed at 5216s: with generation now taking seconds, a run is dominated by judge and RAGAS calls under the 20 requests/minute limiter, not by the orchestrator.

### Findings recorded, not fixed

1. **Three factual questions are answered by denying facts the corpus contains, and they are two different failures.** `false_refusal_rate` is 0.200, from rows 0, 2 and 18. Conflating the causes would send Phase 3 and Phase 4b in the wrong directions.

   **Row 0 is a genuine retrieval miss.** "What is the I-983 and who fills it out?" scores `context_precision` 0.333, and the answer describes the H-1B corpus in response to a STEM OPT question. I-983 is in the database on the "STEM OPT Employer Requirements and Responsibilities" chunk, and retrieval never surfaced it. A form number is exactly the kind of exact token a `tsvector` arm catches and a vector arm misses. This is the Phase 3 before-case for hybrid retrieval.

   **Row 18 is a fact-combination failure, and retrieval succeeded.** "If I use three months of full-time pre-completion OPT, how much time do I get after graduating?" scores `context_precision` 1.0. RAGAS's own judge stated the case in the text of an earlier parse failure: the context "explains the rules for OPT and STEM extensions, specifically detailing how pre-completion OPT reduces the available 12-month post-completion OPT window and how a STEM extension can be added on top", and "the answer correctly calculates the remaining post-completion OPT time and the addition of the STEM extension to reach the total of 33 months." The judge saw the context supported the answer and the system refused anyway. **A second retrieval pass would not fix this**, because there is nothing further to retrieve: the model has both rules in front of it and will not combine them. That bears directly on the Phase 4b decision.

   **Row 9 moved.** "How long is post-completion OPT?" is no longer classified a refusal, but it now scores `faithfulness` 0.5 with `answer_relevancy` 0.0, so it has traded a refusal for a partly ungrounded answer rather than getting the fact right. "Up to 12 months" is the opening sentence of `opt-uscis-opt-f1-students.md` and is in the database. It has failed in some form on four consecutive runs.

2. **RAGAS parse failures persist, and the constraint meant to prevent them appears to cause them.** One this run, on row 16 `faithfulness`: `OutputParserException: Failed to parse StringIO from completion {"statements": [...]}` where the quoted completion is well-formed JSON. RAGAS wants its `StringIO` wrapper on the retry path and gets the metric schema back instead, consistent with `response_format: json_object` pinning the model to the wrong schema. Worth a look in Phase 3, when the eval runs twice.

3. **Row attribution for RAGAS failures was wrong and is fixed.** The previous run labelled a failure `row_index: 15` whose text was unmistakably row 18. The `counter // num_metrics` arithmetic was correct; the bug was one level up. `run.py` builds the RAGAS dataset from scored rows only, so each errored row shifts every later row left in RAGAS's numbering, and row 18 minus 3 missing rows landed at local position 15. Each row now carries its real `golden_index` through, and a missing index reports as `None` rather than a guess. Verified this run: `row_index=13` maps to "What is the annual H-1B cap" and `row_index=16` to "How does the wage-weighted lottery work?", both correct. Note this bug only manifested when rows errored, so it was invisible in exactly the runs where the attribution did not matter.

4. **`answer_relevancy` penalizes correct refusals.** The advice subset scores 0.377, and rows 0, 9 and 18 score 0.0. A refusal is by construction not relevant to the question asked. As Phase 4 adds a refusal path and leakage falls, `answer_relevancy` will fall with it. That is the metric behaving as designed, and the Phase 4 report should say so rather than treat it as a regression.

5. **`unreferenced_citation_rate` measures format compliance, not citation quality.** 77 of 105 returned citations were never referenced. On an earlier run only 7 of 16 non-empty answers used `[n]` brackets; 8 inlined raw URLs and 1 used neither. The prompt does not pin a citation format. This belongs with the Phase 4 citation verifier.

6. **The empty-answer diagnosis, for the record.** Ollama returned HTTP 200 with `content: ""`, `done_reason: 'length'`, `eval_count` 4422 to 6095, and `prompt_eval_count` only about 2097. An output-length cutoff from runaway deliberation, not a context overflow. Two things had to be fixed before this was visible: `main.py` converting the exception to a bare 502 without logging, and `run.py` calling `raise_for_status()`, which discards the body where the message lives.

7. **Ollama keep-alive eviction, and what it was not.** Two back-to-back hour-long runs plus my concurrent third stretched the gaps between rows past Ollama's default 5-minute keep-alive, so nearly every row paid a cold-load cost. `ollama ps` caught `qwen3.5-8k` mid-`Stopping...` while the GPU sat at 48% and 1725 MHz with no thermal throttling, so the machine was never the bottleneck. Fixed by `OLLAMA_KEEP_ALIVE=60m` on the host plus a `keep_alive` field in the request payload so a fresh clone behaves the same. **The timeout increase was not what unblocked this.** Latency figures from runs before that fix are contention artifacts and must not be read as measurements.

### Caveats and not done

- **The judge endpoint, not the orchestrator, sets the run time.** Generation now takes about 6 seconds a row; a full run takes 87 minutes. Everything above the generator is judge and RAGAS traffic under a 20 requests/minute limiter. Phase 3's two runs will cost roughly three hours of wall clock.
- **The PASS exit path is untested**, since no run has passed.
- **Two eval runs were wasted** by my leaving a builder subagent live while starting another. Their latency numbers are unusable. Checking for a running `eval.run` before starting one is now standard and has already caught a fourth unauthorized run.
- `config.py`, `docker-compose.yml`, `.env.example` and `README.md` now all name `qwen3.5-8k:latest`. Before this round, a fresh clone would have run `gemma4` and `README.md` told the user to pull it, so no number in an earlier report would have reproduced.
- ARCHITECTURE.md line 177 was corrected: it documented `gemma4:latest` as the generator long after the configured model became `qwen3.5-8k:latest`.
- Nothing has been committed or pushed.
