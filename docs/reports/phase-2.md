## Phase 2 Verification Report

Status: COMPLETE for everything checkable locally. The workflow file exists, is valid, invokes the real eval entrypoint, needs no secret, and every command it runs has been executed locally and succeeds. Whether it is green on a real pull request and whether branch protection makes it required are human-review items, because both require a push and a GitHub Actions run that I cannot and did not perform.

Loop summary: 2 rounds.
- Round 1: the builder delivered the workflow, stub providers, fixture corpus, CI mode, baselines and the ADR. Verification found two defects it had not caught, one of them an anti-gaming violation.
- Round 2: both fixed and independently re-verified.

### The two defects found in verification

**A gated CI metric was a tautology.** `eval/baselines.json` recorded `false_refusal_rate: 0.0` and `advice_leakage_rate: 0.0` as gated CI baselines. They were 0.0 by construction, not by measurement: `StubLLM` refused exactly when the question matched `_STUB_ADVICE_PATTERNS`, and its refusal text was written (by the builder's own comment) to "deliberately contain phrasing `CI_REFUSAL_PATTERNS` is built to recognize." So the metric measured whether the stub's pattern list agreed with the `is_advice` column. Tested against the golden set, it agreed perfectly: 6 of 6 advice rows matched, 0 of 15 false positives.

A code comment claimed the patterns were "ordinary advice-seeking phrasing... not tuned against any specific golden row." That was untrue for four of them, each matching exactly one row, including `"my odds"` (row 5, "What are my **odds** in the H-1B lottery this year?") and `"will uscis count"` (row 8, "**Will USCIS count** it as a specialty occupation?"), which is near-verbatim from the question.

This mattered because the real system scores `false_refusal_rate` 0.200 and `advice_leakage_rate` 0.833. CI would have printed PASS on two gates the system badly fails, which is precisely the "green CI mistaken for a passing eval" outcome the banner exists to prevent. A banner does not help when the table itself shows the gates passing.

Fixed by removing every single-row pattern (the partition is no longer perfect: 3 of 6 advice rows match, so `advice_leakage_rate` now reads a real 0.5), moving both rates to `REPORTED (stub-derived)` and out of the baseline entirely, and gating instead the structural bookkeeping those rows exercise: `non_advice_scored_count` 15, `advice_scored_count` 6, `unclassified_rows` 0. That catches a genuine regression in refusal bookkeeping without pretending to measure refusal quality. A regression test, `test_stub_advice_patterns_never_match_exactly_one_golden_row`, prevents recurrence.

**The chunking tests were gated out of CI.** A module-level `pytestmark = pytest.mark.full_corpus` on `test_chunking.py` combined with `pytest -m "not full_corpus"` in the workflow deselected all 9. Phase 0's report had recorded that these could not run in CI because `data/sources/raw/` is gitignored, and that "Phase 2's `eval/fixtures/sources/` is where that gets solved." Marking them out was the opposite of solving it, and it meant a pull request that broke the chunker would pass CI.

Fixed by making the tests read whichever directory `RAW_SNAPSHOT_DIR` names, so the same assertions run against fixtures in CI and the full corpus locally. Only the three genuinely full-corpus checks stay marked (the 14-snapshot count and the parametrization over four specific real URLs). No assertion was weakened.

### Machine-checkable gate

- [x] **DoD 1. The workflow exists and is valid YAML** — ran: `python -c "import yaml; yaml.safe_load(open('.github/workflows/eval.yml'))"` — got: parsed successfully, `triggers: ['pull_request', 'workflow_dispatch']`.

- [x] **DoD 2. The job invokes the real eval entrypoint and does not swallow its exit code** — the `ci-invariant-gate` job runs `python -m eval.run --ci`, the real module, not a wrapper script. Grepped the workflow for `continue-on-error` and `|| true`: neither appears on the eval step. The job fails when the eval exits non-zero.

- [x] **DoD 3. Trigger is `pull_request` and the job needs no secret** — ran: parsed the YAML and inspected both jobs — got: `job 'ci-invariant-gate': uses_secrets=False, if=github.event_name == 'pull_request'` and `job 'full-eval': uses_secrets=True, if=github.event_name == 'workflow_dispatch'`. The secret is referenced only on the manually triggered job, so a fork can run the gate.

- [x] **DoD 3b. CI-mode eval runs end to end in under 5 minutes** — ran: `time docker compose exec -T orchestrator python -m eval.run --ci` — got: `real 0m2.057s`, `EXITCODE=0`, `OVERALL: PASS`, 21/21 rows scored, 0 errored.

  **Computed** (real, no external model): `citation_hallucination_rate`, `unreferenced_citation_rate`, `errored_rows`, `empty_answer_rows`, `reading_grade_level`, all three subset breakdowns with denominators, and the refusal bookkeeping counts.
  **Skipped**, recorded in the results JSON as `skipped_metrics: ['faithfulness', 'answer_relevancy', 'context_precision', 'comprehensibility']` and printed as `SKIPPED (CI mode)` on every affected row, with a banner stating no LLM-judged metric ran.
  **Reported but not gated**: `false_refusal_rate` and `advice_leakage_rate`, labelled `REPORTED (stub-derived)`.

- [x] **DoD 3c. No `gemma4` default anywhere CI could pick it up** — ran: `grep -rn "gemma4" --include=*.py --include=*.yml --include=*.yaml --include=*.toml --include=.env.example .` — got: no matches. The only remaining occurrences in the repository are in `docs/reports/phase-0.md` and `phase-1.md`, describing the historical state, which is correct.

- [x] **DoD 4. The eval command passes locally right now** — the run above exited 0 with `OVERALL: PASS`.

- [x] **DoD 5. Every other command the workflow runs also succeeds locally** — ran the workflow's sequence against a throwaway database so the dev corpus was untouched:
  - `psql -f init.sql`: `CREATE EXTENSION`, `CREATE TABLE`, `COMMENT`
  - fixture ingest with `INGEST_MODE=snapshot`: `TOTAL: 17 chunks across 4 sources`, 5 + 4 + 4 + 4
  - row count: `17|4`
  - `pytest -m "not full_corpus"` against the fixture corpus: `40 passed, 6 deselected in 5.02s`, including the 4 chunking invariants
  - full `pytest -v`: 46 passed
  - `ruff check . && black --check .`: clean

- [x] **DoD 6. The README documents the push and branch-protection steps as the user's** — both are written up as manual, with the note that nothing in the repository will do them.

- [x] **Anti-gaming** — `eval/golden.jsonl` unchanged: 21 rows, 15/6 advice, zero placeholders. `THRESHOLDS` byte-identical to Phase 1, and now guarded by `test_thresholds_are_unchanged`. `full_run_reference` in `eval/baselines.json` untouched. The two defects above were both caught and corrected rather than accepted.

### Human-review items (yours, after you push)

- [ ] **The workflow actually runs on a pull request.** Check: open a PR against `main` and look at the Actions tab. What you should see: `ci-invariant-gate` running and going green, with the banner and the `SKIPPED (CI mode)` markers visible in the log.
- [ ] **It goes red on a real failure.** Check: push a commit that breaks something the gate covers, for example changing the chunker's parenting rule or the top-k. What you should see: a red check, with the failing metric named against its baseline.
- [ ] **Branch protection makes the check required.** Check: repository Settings, Branches, protect `main`, mark `ci-invariant-gate` a required status check. This cannot be set from code.
- [ ] **The full eval on `workflow_dispatch`** needs `JUDGE_API_KEY` in repository secrets before it will run.

### Baselines, and what they mean

`eval/baselines.json` holds two separate things, and the distinction is the substance of `docs/adr/0004-ci-baselines-vs-aspirational-thresholds.md`.

`ci_baseline` is the gate. Recorded from a real `--ci` run against a freshly initialised Postgres ingested only from `eval/fixtures/sources/` with stub providers, which is exactly the configuration the `pull_request` job uses. The rule is "no worse than the last accepted run", with an explicit float tolerance. It holds `errored_rows` 0, `empty_answer_rows` 0, `non_advice_scored_count` 15, `advice_scored_count` 6, `unclassified_rows` 0, `citation_hallucination_rate` 0.0, `unreferenced_citation_rate` 0.714, `reading_grade_level` 12.46.

`full_run_reference` is not a gate and never compared against a CI run. It records the Phase 1 full-run numbers so the distance to the aspirational `THRESHOLDS` stays visible: faithfulness 0.910, answer_relevancy 0.529, context_precision 0.914, false_refusal_rate 0.200, advice_leakage_rate 0.833, comprehensibility 2.905, citation_hallucination 0.0, unreferenced 0.733, reading grade 15.638.

`THRESHOLDS` in `eval/run.py` is unchanged and remains the Phase 4 target that only a full run is measured against. Four of them fail today, by design.

### Caveats and not done

- **The `pull_request` gate checks invariants, not answer quality.** This is the honest limit of the phase goal. With no secret and no GPU, CI cannot run a real generator or the judge, so it cannot measure whether an answer got worse. What it does catch: a broken chunker, a citation that references a chunk not retrieved, a change in citation construction or top-k, a row erroring or returning empty, refusal bookkeeping losing rows, and the subset denominators drifting. Real answer-quality gating lives in the `workflow_dispatch` full eval, which you trigger deliberately. The report and the ADR both say this plainly rather than letting a green check imply more than it means.
- **My timed CI run used the dev stack's 216-chunk corpus, not the 4-file fixture corpus.** It exercised the same code path and providers, and produced `unreferenced_citation_rate` 0.657 against a fixture-derived baseline of 0.714, passing because lower is better. In actual CI the corpus is the fixtures, so the numbers line up. The fixture path was verified separately through the ingest dry run and the fixture-scoped pytest run.
- **Provider env vars are read by the orchestrator at startup, not per request.** Setting `LLM_PROVIDER=stub` on the eval process alone silently leaves the orchestrator on Ollama. The workflow sets them at job level so CI is unaffected, and it is documented in `eval/README.md`. Worth remembering for any local CI-mode run: the orchestrator has to be recreated, and restored afterwards.
- **The full-mode baseline comparison print path is code-reviewed but not executed**, because running full mode costs 87 minutes and I declined to spend it on a print statement. It will first execute on the next full run.
- A `.git` directory exists with a branch `phase-2-eval-gate`, which the session environment reported as absent. That is your manual git work. Nothing in this session ran a git or gh command.
- Nothing has been committed or pushed.
