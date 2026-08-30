## Phase 0 Verification Report

Status: COMPLETE (machine gate). One human-review item is open by design: the user authors the real golden set.

Loop summary: 3 rounds.
- Round 1: builder delivered the full scaffold and found a real bug on its own. Four of the fourteen pages render their FAQ sections as a CKEditor accordion (`<dl>/<dt>/<dd>`) rather than heading tags, including the fixed-admission FAQ that the parenting test grades. Plain markdownify turned those into Pandoc-style definition lines the section chunker could not see as boundaries, so those pages collapsed to one chunk. The builder added `promote_definition_lists()` to rewrite `<dt>` as `<h2>` before conversion. My verification then found three defects the builder had not reported.
- Round 2: fixed all three. (a) 57 of 217 rows stored `section_heading` as raw markdown link source, `[What if my M-2 visa expired?](#what-if-my-m-2-visa-expired)`, because the ICE page wraps every heading in a link to its own anchor. (b) A `Local Footer Navigation` chunk from ice.gov was in the retrieval corpus; the block sits in a `div.field--name-field-local-footer` inside `<main>`, which the noise filter did not match. (c) `_find_headings` accepted a heading whose text cleaned away to empty, which would silently set `parent=""` for every chunk after it.
- Round 3: I found that `test_chunking.py` located snapshots by filename stem, and those stems exist only because the prep step hand-named them. `data/sources/raw/` is gitignored, so on a fresh clone `mint_snapshot_filename` generates the names instead, and 12 of 14 come out different. The test passed here for an incidental reason. The builder rekeyed every lookup to `source_url` read from each snapshot's own frontmatter, added an assertion that the snapshot URL set exactly equals `data/sources/sources.yaml`, and fixed the domain hint that was producing `www` for every uscis.gov and ice.gov name.

I then tore the whole thing down (`docker compose down -v`), emptied `data/sources/raw/`, and rebuilt from nothing to confirm every check below on a clean slate rather than on accumulated state.

### Machine-checkable gate (ALL green for COMPLETE)

- [x] **DoD 1. `docker compose up` starts postgres, orchestrator, and otel-lgtm with no errors** — ran: `docker compose down -v && docker compose up -d --build && docker compose ps` — got: `postgres Up (healthy)`, `orchestrator Up`, `otel-lgtm Up (healthy)`; ports 5432, 8000, 4317, 4318, and Grafana published — expected: three services up, no errors.

- [x] **DoD 2. Ingestion reads all 14 manifest entries and every row is fully populated** — ran: `docker compose run --rm orchestrator python -m app.ingest` from an empty database and an empty snapshot directory — got: `TOTAL: 216 chunks across 14 sources`. Robots.txt fetched once per host (uscis.gov 302 to `/sites/default/files/robots.txt` then 200, studyinthestates.dhs.gov 200, ice.gov 200). The redirect resolved live: `GET .../traveling-as-an-international-student "HTTP/1.1 301"` then `GET .../traveling-as-an-f-or-m-student "HTTP/1.1 200"`.
  - `SELECT count(*), count(DISTINCT source_url) FROM documents;` gave `216|14` — expected: 14 distinct sources.
  - `SELECT min(c) FROM (SELECT count(*) c FROM documents GROUP BY source_url) t;` gave `4` — expected: > 0 per source. Range is 4 to 60.
  - `SELECT count(*) FROM documents WHERE source_url IS NULL OR section_heading IS NULL OR heading_level IS NULL OR page_last_updated IS NULL OR fetched_at IS NULL OR last_verified_at IS NULL OR embedding IS NULL;` gave `0` — expected: 0.
  - `SELECT DISTINCT resolved_url FROM documents WHERE resolved_url IS NOT NULL;` gave the single expected row, `https://studyinthestates.dhs.gov/students/study/traveling-as-an-f-or-m-student`.
  - All 14 `page_last_updated` dates parsed from the live pages, in both formats ("Last Reviewed/Updated: 07/17/2026" and "Last updated: April 29, 2025"), and match the values the prep step recorded independently.
  - Idempotency: a second consecutive ingest reported `TOTAL: 216 chunks across 14 sources` and `SELECT count(*)` stayed at `216`, not 432.

- [x] **DoD 2b. The chunking test passes** — ran: `docker compose run --rm orchestrator pytest -v` — got: `10 passed`.

  ```
  tests/test_chunking.py::test_fourteen_snapshots_present PASSED
  tests/test_chunking.py::test_every_snapshot_produces_more_than_one_chunk PASSED
  tests/test_chunking.py::test_h4_only_pages_produce_at_least_four_chunks[extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students] PASSED
  tests/test_chunking.py::test_h4_only_pages_produce_at_least_four_chunks[h-1b-electronic-registration-process] PASSED
  tests/test_chunking.py::test_h4_only_pages_produce_at_least_four_chunks[h-1b-specialty-occupations] PASSED
  tests/test_chunking.py::test_h4_only_pages_produce_at_least_four_chunks[optional-practical-training-extension-for-stem-students-stem-opt] PASSED
  tests/test_chunking.py::test_every_chunk_has_a_non_empty_heading_and_valid_level PASSED
  tests/test_chunking.py::test_fixed_admission_faq_parenting_inverts_correctly PASSED
  tests/test_h1b_electronic_registration_preheading_table_is_captured PASSED
  tests/test_query.py::test_query_happy_path_returns_grounded_citation PASSED
  ```

  The three graded behaviors, each asserted rather than assumed:
  - No source collapses to a single chunk. Minimum across all 14 is 4. The four h4-only USCIS pages, which would each be one chunk under h2-only splitting, produce 12, 11, 12, and 5.
  - The fixed-admission FAQ parents its h2 questions under the right h3 group: "What is the new departure period for F students?" resolves to parent "Departure Period for F Students", "If I am a current student admitted under duration of status, do I need to apply for an extension of stay?" to "Transition Period", and "What does the AUD mean?" to "Understanding the Admit Until Date (AUD)". Looked up by heading text, not by index.
  - The electronic-registration historical table survives: exactly one chunk contains both `274,237` (FY2021) and `358,737` (FY2026), and its heading equals the page title derived from the file's own h1 line rather than a hardcoded string.

  I re-ran this against freshly minted filenames after emptying `data/sources/raw/`, and it passed with test ids like `[h-1b-electronic-registration-process]` resolving through source_url. No test is skipped, xfailed, or trivial: `grep -rn "skip\|xfail" services/orchestrator/tests/` returns nothing.

- [x] **DoD 3. `POST /query` returns 200 with a non-empty answer and a real citation** — ran: `curl -X POST http://localhost:8000/query -d '{"question":"How long is the STEM OPT extension?"}'` — got: `HTTP 200 in 24.66s`, a 5-citation answer opening "Based on the context provided, the STEM OPT extension can be for the following durations: Primary Extension: You may apply for a 24-month extension of post-completion OPT employment authorization...", plus the disclaimer field. I checked each distinct citation URL back against the table: `rows=5`, `rows=7`, `rows=12`. All three are ingested sources.

- [x] **DoD 4. A trace with distinct retrieve and generate spans** — ran: Tempo search through the Grafana datasource proxy, `GET /api/datasources/proxy/uid/tempo/api/search?q={ name="POST /query" }`, then fetched the trace by id — got: trace `1a6d5c249fca6e8573c281a137f48dc5`, 24656ms, matching my curl to the millisecond.

  ```
  POST /query               parent=ROOT        svc=office-hours-orchestrator 24657ms
  generate                  parent=POST /query svc=office-hours-orchestrator 24592ms
        {'model': 'gemma4:latest', 'prompt_chars': '7890', 'answer_chars': '870'}
  retrieve                  parent=POST /query svc=office-hours-orchestrator    64ms
        {'top_k': '5', 'result_count': '5'}
  ```

  Both are direct children of the request root, in one trace, in the same service.

- [x] **DoD 5 (schema half). `eval/golden.jsonl` exists with all six fields documented** — got: 7 rows, every one carrying exactly `question`, `ground_truth_answer`, `source_urls`, `is_advice`, `verified_on`, `volatility`. Every `ground_truth_answer` is the literal placeholder `TEMPLATE - the user writes this answer by hand`; nothing in the build authored an answer. Coverage: stable factual, volatile factual (the 60-day to 30-day departure period), two advice-seeking rows with `is_advice: true`, and one row whose `source_urls` has two entries. All six documented in `eval/README.md`, with the explanation of why `verified_on` and `volatility` exist.

- [x] **DoD 6. `.gitignore` covers the sensitive paths, and no hardcoded credentials** — `.gitignore` covers `.env` and `.env.*` with `!.env.example`, `eval/results/*` with `!eval/results/.gitkeep`, `data/sources/raw/`, `__pycache__/`, `*.pyc`, `.venv/`, `venv/`, `node_modules/`, `.next/`, `.pytest_cache/`, `.ruff_cache/`, `*.egg-info/`, `build/`, `dist/`, `*.log`, and OS/IDE files.
  - Secret scan across every file the build created (`*.py`, `*.yml`, `*.sql`, `*.toml`, `*.json`, `*.jsonl`, `*.md`, `Dockerfile`, `.env*`, `.gitignore`), looking for `sk-`, `ghp_`, `gho_`, `AKIA`, PEM private key headers, and assigned `api_key`/`token`/`password` literals: **no matches**.
  - The only credential-shaped strings anywhere are the dev-default Postgres pair `officehours:officehours`, which appears in `.env.example` (documented as a dev default), in `docker-compose.yml` behind `${POSTGRES_USER:-officehours}` substitution, and as a fallback in `config.py`. `api_key` appears only as an unset constructor parameter on the two hosted-provider stubs.

- [x] **DoD 7. `docs/reports/phase-0.md` contains a copy of this report** — this file.

- [x] **Lint and format** — ran: `ruff check . && black --check .` — got: `All checks passed!` and `14 files would be left unchanged.`

### Human-review items (the user confirms these)

- [ ] **Write roughly 20 real golden rows.** Check: `eval/golden.jsonl`. What you should see: 7 template rows whose `ground_truth_answer` is a placeholder. By your own definition, Phase 0 is not done until you have replaced these with real answers. Nothing in this loop may author or edit them. `data/sources/candidate-questions.md` is the starting list.
- [ ] **Look at the trace in the Grafana UI.** Check: `http://localhost:3000` (or `GRAFANA_HOST_PORT`), Explore, Tempo datasource, search service `office-hours-orchestrator`. What you should see: a `POST /query` trace with `retrieve` and `generate` nested under it, generate taking almost all the time. I verified this through Tempo's HTTP API; whether the flame graph renders the way you want is yours to judge.
- [ ] **Set a real contact in `USER_AGENT`.** Check: `.env.example`, which currently ships `contact: set-a-real-contact-here`. The builder deliberately did not put your email in a header sent to government web servers. Federal sites expect a reachable contact from a crawler; decide what you want there before crawling at any volume.
- [ ] **All repository work.** Nothing here has been committed, staged, or pushed, by design.

### Quality metrics (reported, NOT gated, never optimized against)

The eval harness (`eval/run.py`, `judge.py`, `metrics.py`) does not exist until Phase 1, and the golden set has no real answers yet, so faithfulness, answer relevancy, context precision, false-refusal, advice-leakage, and comprehensibility **cannot be computed in Phase 0**. No numbers are reported because there are none to report; inventing them would be worse than the gap.

What I can report is three probe queries against the running system, as observations of where Phase 4 starts from. These are not eval results and nothing was tuned against them.

- **Temporal answer, the DHS fixed-admission case.** Asked how many days an F-1 student has to leave after finishing their program. The system returned both rules with their dates: the previous 60-day period, the new 30-day period, and "starting on Sept. 15, 2026, F students will be admitted for an additional 30-day period for departure", across 3 distinct sources. The prompt alone is currently producing the both-rules-with-dates behavior the constraint requires.
- **Out of corpus.** Asked for the O-1 artist visa filing fee, which is not in these 14 pages. The system said "Your sources do not cover the current filing fee for an O-1 artist visa petition." It still attached 2 citations to that non-answer, which is wrong and is on the Phase 4 list.
- **Advice-seeking.** Asked "Should I apply for STEM OPT now or wait until my current OPT is closer to expiring?" The system **did not refuse**. It answered with the filing deadlines instead. It avoided telling the person what to do, so it did not leak advice outright, but there is no refusal path in Phase 0 and the system prompt does not produce one on its own. This is the gap the Phase 4 classifier closes, and it is the single most important thing still missing.

### How the core piece works (plain English)

Ingestion fetches each of the 14 government pages over plain HTTP, honoring robots.txt and pausing two seconds between hits to the same host, strips navigation and scripts, and converts what is left to a markdown snapshot saved on disk. The chunker then splits that snapshot at every h2, h3, and h4 heading, because these pages do not agree with each other about which level means what: the USCIS pages put their real section boundaries in h4 with no h2 at all, and the fixed-admission FAQ has its questions in h2 while the group labels holding them are h3. The trick that makes both work is to ignore level numbers entirely for nesting. A heading with nothing under it before the next heading is treated as a label, not a section, and every content chunk that follows carries the most recent label as its parent, purely in document order. Text sitting above the first heading is kept as its own chunk attributed to the page title, which is the only reason the H-1B registration table of FY2021 to FY2026 numbers survives at all. Each chunk gets a breadcrumb line prepended so the embedding and the model both see which group a question belongs to, then goes into one Postgres table with its source URL, its resolved URL after redirects, its heading and level, and a 768-dimension vector. A query is embedded with the same model, Postgres does a plain cosine scan to find the five closest chunks, those go into a prompt that says to answer only from them and cite each claim, and the answer comes back with the citations attached and a disclaimer. The retrieve step and the generate step each open their own OpenTelemetry span inside the request trace, so one Grafana view shows that retrieval took 64ms and the local model took 24 seconds.

### Decisions logged

- `ARCHITECTURE.md` — every Phase 0 decision with its reason, written from the CLAUDE.md constraints, and marked by the phase each one goes live in.
- No files in `docs/adr/` yet. The repository layout allocates `0001` to Phase 3 (RRF versus weighted blend), `0002` to Phase 4 (the advice versus information line), and `0003` to Phase 7 (the Go and Python split). The one Phase 0 decision that would otherwise deserve an ADR, deriving heading nesting from document order and empty headings instead of level numbers, is written up in `ARCHITECTURE.md` under "Parenting comes from document order and empty headings, not from level number". Say the word if you would rather it also be a numbered ADR.

### Caveats / not done

- **Grafana is on port 3001 on this machine, not 3000.** An unrelated `node.exe` (PID 15716) holds 3000. `docker-compose.yml` maps `${GRAFANA_HOST_PORT:-3000}`, so the default is unchanged for anyone else, and the local `.env` overrides it. Free 3000 or keep the override, your call.
- **The chunking test cannot run in CI yet.** It reads `data/sources/raw/`, which is gitignored, so a CI checkout has nothing to chunk. Phase 2's `eval/fixtures/sources/` is where that gets solved. Locally it runs against the real corpus, which is stronger.
- **The prep step's frontmatter annotations live only on this machine.** `heading_note`, `rule_effective_date`, and `federal_register` sit in three gitignored snapshots. Ingestion preserves them across re-crawls (verified), but a fresh clone regenerates the snapshots without them. Nothing reads them today. If `rule_effective_date` should drive behavior, it needs to become a column, which is a Phase 4 or 5 question.
- **A title chunk is recorded at `heading_level` 1 even where the page marks its title as h2.** Seven Study in the States pages have no h1. Every page produces exactly one level-1 chunk, which reads as "page root" consistently, but it is a small normalization of what the markup literally says.
- **Flat parenting has one known wrong answer.** On the full-course-of-study page, "Online courses and Distance Learning" inherits the parent "Full course of study requirements for post-secondary programs" because no later label resets it. This follows directly from the level-free rule, which is the rule that makes the inverted FAQ work. Fixing it needs hierarchy logic that would break the graded page, so it stays and is recorded here instead.
- **An "I do not know" answer still returns citations.** See the second probe above. Phase 4.
- **No advice refusal exists.** See the third probe above. This is the largest functional gap leaving Phase 0.
- **`HostedEmbedder` and `HostedLLM` are stubs** that raise `NotImplementedError` with a clear message. They exist to prove the interface seam, as scoped. The production provider gets chosen in Phase 8.
- Nothing has been committed or pushed.
