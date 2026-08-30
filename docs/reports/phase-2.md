## Phase 2 Verification Report

Status: COMPLETE, confirmed by a green pull request run on GitHub. Run #2 passed in 45 seconds: the eval gate step in 3s, `pytest -m "not full_corpus"` in 2.45s with 40 passed and 6 deselected. Every Definition-of-Done item is met, and the human-review items that required a real Actions run are now satisfied except branch protection, which is a repository setting.

Loop summary: 3 rounds.
- Round 1: the builder delivered the workflow, stub providers, fixture corpus, CI mode, baselines and the ADR. Verification found two defects it had not caught, one of them an anti-gaming violation.
- Round 2: both fixed and re-verified.
- Round 3: the first real pull request run went red on `ModuleNotFoundError: No module named 'langchain_core'`. Fixed with lazy imports, re-verified in an environment that genuinely lacks the heavy dependencies, and confirmed green on GitHub.

### Round 3: the CI failure, and why local verification missed it

The first pull request run died before it could do any work:

```
File "eval/run.py", line 77, in <module>
    from eval.judge import (
File "eval/judge.py", line 22, in <module>
    from langchain_core.rate_limiters import InMemoryRateLimiter
ModuleNotFoundError: No module named 'langchain_core'
```

`eval/judge.py` built a module-level `SHARED_RATE_LIMITER` from `langchain_core`, and `eval/run.py` imported `eval.judge` at module scope, so `--ci` died on import before reaching any CI branch. The `eval-ci` extra installs only `openai` and `textstat`; not installing the RAGAS and LangChain stack is the entire point of CI mode.

Fixed with `get_shared_rate_limiter()`, a memoised accessor that imports `langchain_core` on first call and still returns one limiter shared between the judge and RAGAS. `run.py`'s module-scope import is now just `EmptyAnswerError`; the judge functions are imported inside the full-mode branch.

**The verification miss is the part worth recording.** The original report marked DoD 5, "every command the workflow runs also runs locally and succeeds," as verified. Those commands were run inside the orchestrator container, which is built from the fat `[dev,eval]` extra. That check was not merely unverified, it was **structurally incapable** of catching a missing-dependency failure: an environment with more dependencies than the target cannot detect a dependency being absent from the target, no matter how carefully the output is read. The command ran; the environment did not match.

This applies to every future Definition-of-Done item that claims a workflow command works locally. "Locally" and "as CI runs it" are different questions, and only the second one is evidence. For this repository the honest reproduction is a throwaway `python:3.12-slim` with only `[dev,eval-ci]` installed, never `docker compose exec orchestrator`.

**A property of the two-job design, worth knowing.** Only the `pull_request` path was broken. The `workflow_dispatch` full-eval job installs `[dev,eval]`, so it would have passed throughout. A green manual full eval would never have revealed that the automatic gate was broken. The two jobs have different dependency sets by design, which means they can fail independently and neither one vouches for the other.

**The broadened guard test is the durable artifact here, worth more than the fix.** The original guard probed `sys.modules` for one name:

```python
probe = "import eval.run, sys; sys.exit(1 if 'ragas' in sys.modules else 0)"
```

`langchain_core` was never in that list, so it passed while the module-scope import sat right there. It is now `test_importing_eval_run_never_imports_ragas_or_langchain`, probing `ragas`, `langchain_core`, `langchain` and `langchain_openai`, and reporting which one leaked. The reason this matters more than the lazy-import fix: a `sys.modules` probe checks whether a module **got imported**, not whether it is **installed**, so the broadened version would have failed inside the fat orchestrator container, in the very environment where the flawed verification ran. The fix addresses one import. The guard addresses the category, and it runs on every pull request.

### The two defects found in Round 1 verification

**A gated CI metric was a tautology.** `eval/baselines.json` recorded `false_refusal_rate: 0.0` and `advice_leakage_rate: 0.0` as gated CI baselines. They were 0.0 by construction, not by measurement: `StubLLM` refused exactly when the question matched `_STUB_ADVICE_PATTERNS`, and its refusal text was written (by the builder's own comment) to "deliberately contain phrasing `CI_REFUSAL_PATTERNS` is built to recognize." So the metric measured whether the stub's pattern list agreed with the `is_advice` column. Tested against the golden set, it agreed perfectly: 6 of 6 advice rows matched, 0 of 15 false positives.

A code comment claimed the patterns were "ordinary advice-seeking phrasing... not tuned against any specific golden row." That was untrue for four of them, each matching exactly one row, including `"my odds"` (row 5, "What are my **odds** in the H-1B lottery this year?") and `"will uscis count"` (row 8, "**Will USCIS count** it as a specialty occupation?"), near-verbatim from the question.

This mattered because the real system scores `false_refusal_rate` 0.200 and `advice_leakage_rate` 0.833. CI would have printed PASS on two gates the system badly fails, which is exactly the "green CI mistaken for a passing eval" outcome the banner exists to prevent. A banner does not help when the table itself shows the gates passing.

Fixed by removing every single-row pattern (the partition is no longer perfect: 3 of 6 advice rows match, so `advice_leakage_rate` now reads a real 0.5), moving both rates to `REPORTED (stub-derived)` and out of the baseline entirely, and gating instead the structural bookkeeping those rows exercise: `non_advice_scored_count` 15, `advice_scored_count` 6, `unclassified_rows` 0.

**The chunking tests were gated out of CI.** A module-level `pytestmark = pytest.mark.full_corpus` on `test_chunking.py` combined with `pytest -m "not full_corpus"` deselected all 9. Phase 0's report had recorded that these could not run in CI because `data/sources/raw/` is gitignored, and that "Phase 2's `eval/fixtures/sources/` is where that gets solved." Marking them out was the opposite of solving it, and it meant a pull request that broke the chunker would pass CI.

Fixed by making the tests read whichever directory `RAW_SNAPSHOT_DIR` names, so the same assertions run against fixtures in CI and the full corpus locally. Only the three genuinely full-corpus checks stay marked. No assertion was weakened.

**Both defects now have regression tests running on every pull request.** `test_stub_advice_patterns_never_match_exactly_one_golden_row` prevents the tautology from creeping back, and the four chunking invariants prevent the chunker from silently regressing. Both executed on GitHub in run #2.

### Machine-checkable gate

- [x] **DoD 1. The workflow exists and is valid YAML** — ran: `python -c "import yaml; yaml.safe_load(open('.github/workflows/eval.yml'))"` — got: parsed successfully, `triggers: ['pull_request', 'workflow_dispatch']`.

- [x] **DoD 2. The job invokes the real eval entrypoint and does not swallow its exit code** — the `ci-invariant-gate` job runs `python -m eval.run --ci`, the real module. No `continue-on-error` and no `|| true` on the eval step. Proven twice over: the job went red when the eval failed on import, and green when it passed.

- [x] **DoD 3. Trigger is `pull_request` and the job needs no secret** — `job 'ci-invariant-gate': uses_secrets=False, if=github.event_name == 'pull_request'`; `job 'full-eval': uses_secrets=True, if=github.event_name == 'workflow_dispatch'`. Confirmed on GitHub: run #2 completed with no repository secret available to it.

- [x] **DoD 3b. CI-mode eval runs end to end in under 5 minutes** — on GitHub: the eval gate step took **3 seconds**, the whole job 45 seconds.

  **Computed** (real, no external model): `citation_hallucination_rate`, `unreferenced_citation_rate`, `errored_rows`, `empty_answer_rows`, `reading_grade_level`, all three subset breakdowns with denominators, and the refusal bookkeeping counts.
  **Skipped**, recorded as `skipped_metrics: ['faithfulness', 'answer_relevancy', 'context_precision', 'comprehensibility']` and printed as `SKIPPED (CI mode)` on every affected row, with a banner stating no LLM-judged metric ran.
  **Reported but not gated**: `false_refusal_rate` and `advice_leakage_rate`, labelled `REPORTED (stub-derived)`.

- [x] **DoD 3c. No `gemma4` default anywhere CI could pick it up** — ran: `grep -rn "gemma4" --include=*.py --include=*.yml --include=*.yaml --include=*.toml --include=.env.example .` — got: no matches. The only remaining occurrences are in `docs/reports/phase-0.md` and `phase-1.md`, describing the historical state.

- [x] **DoD 4. The eval command passes** — exit 0 on GitHub, and exit 0 in a local clean-room reproduction.

- [x] **DoD 5. Every other command the workflow runs also succeeds** — this is the item that was wrong in the original report, and it is now verified the only way it can be. See Round 3 above for why the original check could not have worked.

  Re-verified in a throwaway `python:3.12-slim` container with only `[dev,eval-ci]` installed, on the compose network, against a throwaway database so the dev corpus was untouched:
  - dependency absence confirmed: `langchain_core`, `langchain`, `langchain_openai`, `ragas` all `ModuleNotFoundError`; `openai` and `textstat` present
  - `applied init.sql`
  - fixture ingest: `TOTAL: 17 chunks across 4 sources`
  - uvicorn started the way the workflow starts it, `/health` reachable
  - `python -m eval.run --ci`: `EXITCODE=0`, `OVERALL: PASS`
  - `pytest -m "not full_corpus"`: `40 passed, 6 deselected`

  Then confirmed on GitHub in run #2, which is the evidence that counts.

- [x] **DoD 6. The README documents the push and branch-protection steps as the user's** — both written up as manual, with the note that nothing in the repository will do them.

- [x] **Anti-gaming** — `eval/golden.jsonl` unchanged: 21 rows, 15/6 advice, zero placeholders. `THRESHOLDS` byte-identical to Phase 1, guarded by `test_thresholds_are_unchanged`. `full_run_reference` in `eval/baselines.json` untouched at `advice_leakage_rate` 0.833. Both Round 1 defects were caught and corrected rather than accepted.

### Human-review items

- [x] **The workflow runs on a pull request and is green** — run #2, 45 seconds, all steps passed.
- [x] **It goes red on a real failure** — demonstrated involuntarily by run #1, which failed on the import error and correctly blocked.
- [x] **The pytest step runs on GitHub** — it was skipped on the red run because the eval step failed first, so run #2 is the first time it executed on a real runner. All four chunking invariants ran there for the first time, including `test_fixed_admission_faq_transition_and_aud_parenting` and `test_h1b_electronic_registration_preheading_table_is_captured`, alongside both anti-gaming guards, `test_stub_advice_patterns_never_match_exactly_one_golden_row` and `test_importing_eval_run_never_imports_ragas_or_langchain`. Result: 40 passed, 6 deselected in 2.45s.
- [ ] **Branch protection makes the check required.** Check: repository Settings, Branches, protect `main`, mark `ci-invariant-gate` a required status check. This cannot be set from code and is the one item still open.
- [ ] **The full eval on `workflow_dispatch`** needs `JUDGE_API_KEY` in repository secrets before it will run.

### Baselines, and what they mean

`eval/baselines.json` holds two separate things, and the distinction is the substance of `docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md`.

`ci_baseline` is the gate. Recorded from a real `--ci` run against a freshly initialised Postgres ingested only from `eval/fixtures/sources/` with stub providers, exactly the configuration the `pull_request` job uses. The rule is "no worse than the last accepted run", with an explicit float tolerance. It holds `errored_rows` 0, `empty_answer_rows` 0, `non_advice_scored_count` 15, `advice_scored_count` 6, `unclassified_rows` 0, `citation_hallucination_rate` 0.0, `unreferenced_citation_rate` 0.714, `reading_grade_level` 12.46.

`full_run_reference` is not a gate and is never compared against a CI run. It records the Phase 1 full-run numbers so the distance to the aspirational `THRESHOLDS` stays visible: faithfulness 0.910, answer_relevancy 0.529, context_precision 0.914, false_refusal_rate 0.200, advice_leakage_rate 0.833, comprehensibility 2.905, citation_hallucination 0.0, unreferenced 0.733, reading grade 15.638.

`THRESHOLDS` in `eval/run.py` is unchanged and remains the Phase 4 target that only a full run is measured against. Four of them fail today, by design.

### Caveats and not done

- **The `pull_request` gate checks invariants, not answer quality.** This is the honest limit of the phase goal. With no secret and no GPU, CI cannot run a real generator or the judge, so it cannot measure whether an answer got worse. What it does catch: a broken chunker, a citation referencing a chunk not retrieved, a change in citation construction or top-k, a row erroring or returning empty, refusal bookkeeping losing rows, and subset denominators drifting. Real answer-quality gating lives in the `workflow_dispatch` full eval. The report and the ADR both say this plainly rather than letting a green check imply more than it means.
- **Provider env vars are read by the orchestrator at startup, not per request.** Setting `LLM_PROVIDER=stub` on the eval process alone silently leaves the orchestrator on Ollama. The workflow sets them at job level so CI is unaffected, and it is documented in `eval/README.md`.
- **The full-mode baseline comparison print path is code-reviewed but not executed**, because a full run costs 87 minutes. It will first execute on the next full run.
- A `.git` directory exists with a branch `phase-2-eval-gate`. That is the user's manual git work. Nothing in this session ran a git or gh command.
- Nothing has been committed or pushed by this session, including this report.
