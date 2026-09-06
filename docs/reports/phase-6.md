# Phase 6 Verification Report

Status: COMPLETE

Loop summary: 6 rounds.

Round 1 built `services/frontend` (Next.js 14 App Router, TypeScript, Tailwind) plus the additive
backend needed to make the wait honest: `POST /query/stream`, `GET /sources/status`, CORS, and an
optional `on_event` callback on `answer_question`. I verified it with Playwright against a
production build and found seven real defects, four of them against explicit requirements. The most
serious was Definition-of-Done item 2 failing outright on mobile: at 375x812 the only disclaimer sat
at y=1127 in an 812px viewport, so the "not legal advice" framing was below the fold on the exact
device class this audience uses. I also found missing non-breaking hyphens on four of the five
states, a literal `--` rendering in handoff copy, a flexbox gap splitting the maker line, a missing
62ch measure cap, duplicate bare-domain labels in a multi-source dated notice, and a favicon 404.

Round 2 fixed all seven. One item in my round-1 list was wrong and I withdrew it: I flagged the
example chip in `components/Chat.tsx:35` for ASCII hyphens after reading the source string, without
checking that it is rendered through `withNonBreakingHyphens` at the call site. It was already
correct. I corrected that before the builder acted on it.

Round 3 made two copy changes you asked for after reading the round-2 report, both to cut
duplication: the per-answer disclaimer paragraph came out of all five response renderers, and the
false "Usually 20 to 40 seconds" line came out of the waiting state. I re-ran items 2 and 6, since
both touch the views that changed.

Round 4 removed the footer disclaimer's `max-w-[76ch]` cap so the standing disclaimer sits on one
line on a desktop window. I re-ran items 2 and 3, since both touch the footer.

Round 5 fixed a real correctness defect in the freshness indicator and closed a navigation gap. The
defect is worth stating plainly: `GET /sources/status` computed `max(last_verified_at)` — the NEWEST
verification across the corpus — so a single freshly re-crawled source could have made the header
claim "Sources checked today" while the other thirteen sat stale. The indicator was reading real
data the whole time; it was reading the wrong aggregate. It now takes the oldest, and the band
decision moved into a pure function in `app/guardrails/freshness.py` where pytest can reach it, so
the frontend renders a verdict rather than computing a freshness claim of its own. Round 5 also
shortened the footer, made the top header row sticky, made the wordmark a home link, put the view
state in the URL so browser back and forward work, and replaced the "Ask a follow-up" placeholder.

Round 6 closed out two things. The freshness indicator's `recent` band was wrapping the sticky bar
to 91px on mobile; it now holds at the chosen 60px across the whole 1-to-7-day range, leaving stale
as the only state that grows. And the report gained the pattern note below, because the same failure
had by then appeared three times in one surface.

Everything below I ran myself against the running stack. I did not take the builder's summary for
any of it.

## Machine-checkable gate (ALL green for COMPLETE)

- [x] **1. The UI loads, sends a query, and renders the answer with inline `[n]` citations whose
  href resolves to a real ingested `source_url` on an official `.gov` domain** — submitted "How many
  days do I have to leave the US after my OPT ends?" through the browser and read the DOM back:
  `inlineCitationCount: 5`, `allGovLinks: 9`, `nonGovLinks: []`. Confirmed the cited URLs are really
  in the corpus rather than plausible-looking, by counting their chunks in Postgres:

      .../fixed-time-period-of-admission-...-faq      -> 45 chunks
      .../f-1-optional-practical-training-opt          -> 22 chunks
      .../fixed-time-period-of-admission-...-quick     ->  8 chunks

  On `/?mock=answer` the inline markers resolve to `www.uscis.gov` STEM OPT, cap-gap, and OPT pages.
  Markers render as `[1]`, `[2]`, comma groups intact; nothing stripped or renumbered.

- [x] **2. The disclaimer is visible on the page without scrolling** — FAILED in round 1, fixed in
  round 2, re-verified in round 3 after the disclaimer was de-duplicated. Measured at a 375px
  content width, 812px viewport:

      page                       disclaimerTop  visibleWithoutScroll  perAnswerParagraph  disclaimers
      /?mock=answer                        69           true              removed              2
      /?mock=refusal_advice                69           true              removed              2
      /?mock=clarify                       69           true              removed              2
      /?mock=no_answer                     69           true              removed              2
      /?mock=blocked_unverified            69           true              removed              2

      text: "Unofficial tool. Not legal advice. Not affiliated with USCIS."   top: 69  bottom: 87

  It lives in the header band, so it does not depend on the page being short. Round 3 removed the
  per-answer disclaimer paragraph that used to sit under every response, leaving two on a page: the
  header band and the footer. That paragraph was never what satisfied this item anyway. On
  `/?mock=answer` at 375px it sat at y=1416 and the footer at y=1665, both below an 812px fold, so
  the header band was already doing all of the work. `components/Disclaimer.tsx` is now the single
  source of that banner and is rendered once by `Header.tsx`.

  Round 5 made the top header row sticky (`sticky top-0 z-40`, 60px), so the wordmark, the
  "Unofficial" tag, and the freshness indicator stay in view at every scroll position. You chose the
  slim row over sticking the full disclaimer band as well, so the "Unofficial tool. Not legal advice."
  line still scrolls away; the word "Unofficial" remains pinned. Verified scrolled 1200px into the
  2851px refusal: the bar holds at `top: 0`, `elementFromPoint` inside its bounds resolves to the bar
  itself rather than bled-through content, and the ask input sits at y=583 well clear of the 60px bar
  and remains hit-testable and focusable.

  Re-verified in round 4 after the footer width cap was removed: `disclaimerTop: 69`,
  `visibleWithoutScroll: true`, `disclaimerCount: 2` on `/` and on `/?mock=refusal_advice` at 375px.
  The footer change cannot move this banner, which sits above it in the header, but the item was
  re-run because the footer is one of the two disclaimers on the page.

- [x] **3. The layout is usable at a 375px-wide viewport with no horizontal scroll** —
  `contentWidth: 375, scrollWidth: 375, horizontalScroll: false, overflowingCount: 0`. Zero elements
  extend past the viewport. Also checked at a 360px content width, which is stricter and also clean.
  Re-verified in round 4 after the footer width cap was removed, on `/` and on
  `/?mock=refusal_advice` (the tallest state): still `horizontalScroll: false` and
  `overflowingElements: 0`, with the footer wrapping naturally to 4 lines at 319px wide inside the
  339px available. Re-verified again in round 5 with the sticky header and the shortened footer:
  `overflowing: 0`, `horizontalScroll: false`.

- [x] **Navigation (round 5): the wordmark returns home and browser back works from every state** —
  the wordmark is a real `<button>` with `aria-label="Office Hours home"` calling
  `useRouter().push("/")`; the "Unofficial" tag stays a non-interactive `<span>`. Tested on all five:

      state                       wordmark -> home   back restores the state
      ?mock=answer                      yes                  yes
      ?mock=refusal_advice              yes                  yes
      ?mock=clarify                     yes                  yes
      ?mock=no_answer                   yes                  yes
      ?mock=blocked_unverified          yes                  yes

  View state now lives in the URL. Two real queries against the live generator produced
  `?q=How%20long%20is%20the%20STEM%20OPT%20extension%3F` then
  `?q=What%20is%20Form%20I-983%20used%20for%3F`, with exactly 2 calls to `/query/stream`. Browser
  back restored the first answer and forward restored the second **with the call count still at 2**,
  so history navigation is served from the in-memory cache and never re-queries. A cold load of a
  shared `?q=` link has no cache entry and runs the query for real, which is the honest behaviour:
  it re-answers from the sources as they stand at that moment.

- [x] **4. All five response states render correctly and distinctly, none as error text** —
  captured each. `answer` and `refusal_advice` from real live queries, the rest from `?mock=`:

      answer              serif prose, inline [n], "Where this came from", Answered <date>
      refusal_advice      the general rule WITH citations first, then the dated notice, then the
                          sand handoff "Your situation needs a person, not a page"
      clarify             the single question, input present and focused (activeElement is the input)
      no_answer           plain statement + "Where to look instead" handoff
      blocked_unverified  plain statement + "This answer didn't pass our citation check" handoff

  There are FIVE, not the four the phase brief listed. `BLOCKED_UNVERIFIED` (`app/schemas.py`) is
  reachable whenever citation verification fails, and `app/pipeline.py` returns it with real
  citations attached, so it needed its own first-class treatment. `Message.tsx` switches on
  `response_type` off the wire and never infers state from prose; an unknown future value falls back
  to plain prose plus the disclaimer rather than crashing.

- [x] **5. The freshness block renders, and an answer whose source carries a `rule_effective_date`
  shows the dated notice with its link** — real query, live generator. Per-source freshness renders
  plainly in "Where this came from" (`studyinthestates.dhs.gov · page updated Aug 31, 2026 ·
  verified today`). The dated notice renders in its own sand block with the saffron left border and
  clock icon:

      "A rule affecting this answer takes effect on September 15, 2026. the first source, the second source"
      -> https://studyinthestates.dhs.gov/final-rule-...-faq
      -> https://studyinthestates.dhs.gov/final-rule-...-quick

  Two distinct URLs with distinguishing labels. In round 1 both printed as the bare string
  "studyinthestates.dhs.gov" twice, because two different pages share one hostname. The
  single-notice case still reads "Read the source", matching the prototype.

- [x] **6. Something informative appears within 2 seconds; no undifferentiated spinner for more
  than 5 seconds** — measured over 6 real queries end to end in the browser, re-run in round 3 after
  the waiting-state copy changed:

      time to first content (stage view + echoed question)   p50   7.8 ms   p95   14.7 ms
      time to "Found N official sources" (real count, wire)  p50   1466 ms  p95   1554 ms
      time to the full verified answer                       p50    7.7 s   p95   13.6 s

  Per-query TTFC: 14.7, 8.9, 13.6, 4.5, 7.8, 4.2 ms. An earlier run on a less loaded machine gave
  p50 2.9 ms / p95 7.9 ms for TTFC and p50 5.1 s / p95 10.4 s for the full answer, so treat the
  answer figures as varying with machine load by roughly 50 percent. Both runs clear the 2-second
  bar by three orders of magnitude on first content.

  There is never an undifferentiated spinner: within milliseconds the page shows four named stages,
  and the retrieve line is then replaced with the real server-reported count. Captured mid-flight:

      Read your question                        (tick)
      Found 5 official sources                  (tick, count off the wire)
      Writing the answer from those sources     (spinner)
      Checking every claim has a citation       (dot)
      Answers are written from the sources each time, not recalled from memory. 9s elapsed.

  The progress bar is `doneCount / STAGES.length`, driven only by received stage events. The one
  moving element not tied to a stage is the elapsed-seconds counter, which is a measured quantity.
  There is no `setInterval`-driven fill anywhere; the prototype's mock fill was deliberately not
  copied. `aria-live="polite"` on the stage list, and `motion-reduce:transition-none` on the bar.

- [x] **7. It works end to end against the running service** — 6 of 6 real browser queries returned
  rendered answers against Postgres (221 chunks, 14 sources), the orchestrator, and Ollama
  `qwen3.5-8k`. Non-determinism confirmed in the wild: "Can I travel outside the US while my OPT
  application is pending?" returned `answer` on one run and `refusal_advice` on another. Both
  rendered correctly, which is what the "never assume a fixed answer shape" requirement is for.

- [x] **8. Production dependency count** — exactly three, ran `npm ls --prod --depth=0`:

      next@14.2.35, react@18.3.1, react-dom@18.3.1

  Nothing beyond Next, React, and React DOM. **No shadcn/ui, no Radix, no Motion, no icon package,
  no markdown renderer, no state library.** Icons are the prototype's inline SVGs; the design needs
  no dropdown, dialog, or combobox, so a component library would have added dependency weight for
  nothing. Dev dependencies are the standard set: typescript, @types/*, tailwindcss 3, postcss,
  autoprefixer, eslint, eslint-config-next. I verified no Playwright leaked in from the builder's
  own verification: `npm ls playwright` returns empty and `dependencies` is byte-identical to the
  three above.

- [x] **9. `.gitignore` covers node_modules and build output, and the untracked count does not
  explode** — I could not run the literal command. `.claude/settings.json` denies `Bash(git:*)` and
  `Bash(git *)`, which CLAUDE.md describes as a deliberate hard block, so `git status --porcelain`
  is unavailable to this session by design. I computed the equivalent by walking the tree and
  applying the `.gitignore` patterns directly:

      services/frontend files that WOULD be newly untracked:  31
      files correctly excluded by .gitignore (whole repo):    20,979

  The 31 are all hand-written source: 4 under `app/`, 10 components, 6 `lib/` modules, and the
  config/Docker files, plus `package-lock.json`. `node_modules/` (389 packages) and `.next/` are
  matched by bare patterns that apply at any depth, and round 1 added `services/frontend/out/`,
  `next-env.d.ts`, `.turbo/`, and `*.tsbuildinfo` before `npm install` was ever run.
  **You should still run the real command once before staging.**

- [x] **10. ci-invariant-gate still passes** — ran both workflow steps in `oh-p5-ci`, the
  dependency-matched `python:3.12-slim` container with only `[dev,eval-ci]` installed, against a
  fixture database, with `SOURCES_MANIFEST_PATH` unset exactly as CI leaves it:

      python -m eval.run --ci      ->  OVERALL: PASS       EXIT_eval_run_ci=0
      pytest -m "not full_corpus"  ->  113 passed, 6 skipped, 11 deselected

  I took a baseline BEFORE any Phase 6 work for comparison: `96 passed, 6 skipped, 11 deselected`,
  `EXIT_eval_run_ci=0`. The deselected count is unchanged and the skip count is unchanged across
  every round, so 96 -> 101 -> 113 is 5 streaming/status tests then 12 freshness-indicator tests
  added, and nothing was deselected, skipped, or weakened to get here.

  On the one test-marker change: `tests/test_query.py`'s module-level `live_stack` mark became a
  decorator on the original happy-path test. That test is the only one it ever covered, so its
  behaviour is identical, and the 5 new tests correctly do not carry it because they run in-process
  against `app.main.app` with stub providers. I checked the counts rather than the explanation.

## The freshness indicator (round 5, verified against real database values)

The header's trust indicator is the one claim the product makes about its own currency, so it was
tested by moving the data underneath it rather than by reading the code. One source of fourteen was
aged while the other thirteen stayed fresh, which is exactly the case the old `max()` got wrong.

    database state                          API freshness_state  stale  rendered indicator                  dot
    all 14 verified 13.7h ago               current                  0  "Sources checked today"             yes
    1 of 14 aged to 30h, 13 fresh today     recent                   1  "Sources checked 1 day ago"         yes
    1 of 14 aged to 22 Aug, 13 fresh today  stale                    1  "Sources last checked 22 Aug 2026"  NO

The middle row is the proof that matters. Thirteen sources were verified today and
`newest_verified_at` still read `2026-09-06T03:26:45Z`, so the old `max()` logic would have said
"Sources checked today" there. It now reports the weakest link. `oldest_verified_at` matched the true
`min(last_verified_at)` in `documents` exactly at every step, and the database was restored to its
exact original values afterward (`min 03:25:37.253785`, `max 03:26:45.145169`, 221 chunks).

The dot is dropped entirely in the stale band rather than recoloured, because an amber dot would
introduce a third hue the design forbids and a saffron dot beside a three-week-old date reads as an
all-clear.

**Dropping the dot opened a hole that a later check caught: on mobile the stale state was invisible.**
The indicator text carried `hidden sm:inline`, so below 640px it rendered at 0 width, and the stale
band has no dot by design. At 375px the sticky bar therefore read only "Office Hours | Unofficial" —
a stale corpus produced silence on the platform this audience actually uses, and silence reads as
"fine". That is the same failure mode as the `max()` bug: the system failing to communicate
staleness. Fixed by rendering a terse label below `sm` in every band, so the indicator is never zero-width.
Round 6 then held the `recent` band on one line too. Measured at 375px against the real database,
by aging one source and reading the rendered bar back:

    band            sticky bar text                                     indicator  bar height
    current         "Office Hours | Unofficial | Checked today"              93px        60px
    recent, 1 day   "Office Hours | Unofficial | 1 day ago"                 64px        60px
    recent, 6 days  "Office Hours | Unofficial | 6 days ago"                70px        60px
    stale           "Office Hours | Unofficial | Checked 22 Aug 2026"      124px        91px

The bar holds at the chosen 60px in every band except stale, which wraps to 91px by design: that is
the one state that has something the reader needs to stop and read.

Getting there needed more than the obvious 3px. Trimming the mobile gap from 7px to 4px rescued only
the single "Checked 1 day ago" string, and by 0.9px, which is the same knife-edge margin already
removed from the footer; every other value in the band ("Checked 2 days ago" through "Checked 7 days
ago", all 121.2px against 116.1px free) still wrapped. Since a weekly refresh cadence puts the corpus
at 2 to 6 days old most of the time, that would have left the bar at 91px in the common case. Dropping
the verb from the mobile recent label is what actually holds the whole 1-to-7-day band, with 43px of
headroom instead of a fraction of a pixel. An abbreviation like "7d ago" also fit, and was rejected:
this audience is largely non-native English readers and CLAUDE.md's plain-language rule rules it out.
The full labels at `sm` and above are unchanged.

Twelve tests cover this: all four bands, both exact boundaries (24h and 168h) and one second past
each, a future timestamp from clock skew, that `oldest_verified_at` is the true minimum and not the
maximum, and the invariant itself — if the endpoint reports "current" then `stale_source_count` must
be 0 AND the true `min(last_verified_at)`, read directly from `documents` rather than from the
endpoint's own number, must be within 24 hours. That test is an implication, so it is only
meaningful while the corpus is in the "current" band; it is, so its assertions execute.

### The pattern worth carrying forward: silence reads as fine

Three separate defects in this one surface turned out to be the same failure, and naming it is more
useful than the three fixes:

1. **The risk you raised first** — that "Sources checked today" might be a hardcoded string. It was
   not, but the question was the right one to ask, and asking it is what found the next two.
2. **`max(last_verified_at)`** — the endpoint reported the freshest source instead of the stalest, so
   thirteen stale sources could hide behind one fresh one.
3. **`hidden sm:inline` on the stale label** — the warning existed in the DOM at zero width on
   phones, and the stale band has no dot, so the whole signal vanished on mobile.

None of these misreported a date. Each one made the system quietly fail to say it was stale, and in
every case the fresh, healthy state looked perfect. That is the trap: a freshness surface tested only
against healthy data passes every time, because healthy data is exactly when it has nothing hard to
say. The bug only exists in the state nobody thinks to open.

So the rule for any future freshness surface, and for the Phase 5 refresh job's own reporting: test
it by making things wrong. Age a source past the threshold, point it at an empty corpus, break the
status call, and ask what the user sees. If the answer is "nothing", that is a defect, not a
non-event, because a person reading rules about their own immigration status will read an absent
warning as an all-clear. The invariant test added in round 5 encodes exactly this for the one case we
can state mechanically: "current" may never be claimed while any source is over 24 hours old. Prefer
that shape of test, an assertion about what the system may never claim, over one that checks today's
happy value.

**The other two on-screen state claims were audited at the same time and are both real.** The
per-source "verified today" lines under each citation read `freshness.sources[].last_verified_at`
for that specific URL, built from the chunks actually retrieved for that query, so the claim attached
to a specific government page is that page's own. "Found N official sources" is `len(chunks)` off the
SSE retrieve event. Nothing on screen is decorative.

## Human-review items (you confirm these)

- [ ] **Run the real untracked-file count before staging** — `git status --porcelain | grep -c "^??"`
  from the repo root. Expect roughly 31 new paths under `services/frontend/` plus the new docs, and
  nothing from `node_modules/` or `.next/`.
- [ ] **The visual result against your prototype** — open `http://localhost:3000/` beside
  `docs/design/office-hours-ui-final.html`. Palette, both type faces, the two-column hero with the
  real cited answer card, the maker line, and the chip strip all match; I checked them
  programmatically but you decided the design and should confirm the feel.
- [ ] **CI on the pull request** — Definition-of-Done item 10 depends on a GitHub Actions run, which
  cannot be machine-checked from here. I ran both of that job's steps locally in a dependency-matched
  Linux container; the workflow run itself is yours.

## Quality metrics (reported, NOT gated, never optimized against)

Phase 6 changed no retrieval, prompt, generation, or guardrail logic, so these are Phase 5's numbers
carried forward unchanged. Run of record `eval/results/20260906T032306Z.json`.

    faithfulness 0.853   answer_relevancy 0.524   context_precision 0.892
    false_refusal 0.200  advice_leakage 0.500     comprehensibility 3.000
    citation_hallucination 0.000                  reading_grade_level 17.079

Typography is the one lever this phase touched, and it is set as specified: 18px serif at
line-height 1.85 with `max-width: 62ch`, measuring 670px and about 70 characters per line at desktop.
That helps a reader absorb dense prose, but it cannot move `reading_grade_level` (17.1 against a
target of 12) or `comprehensibility` (3.0 against 3.5), which are properties of the generated text.
Those remain Phase 4's to improve, and I did not touch the metrics or the rubric.

## How the core piece works (plain English)

A query takes seconds, and the obvious fix is to stream the generator's tokens the way every chat
app does. We cannot, and the reason is structural. The pipeline verifies citations *after*
generation: if an answer cites a source that was never retrieved, `verify_citations` throws the text
away and returns a safe message instead. Streaming tokens would put that text on screen before the
check ran, so a failed answer would have to be retracted from someone already reading it. CLAUDE.md
says nothing uncited renders, so token streaming is off the table.

So the UI streams progress instead of prose. `POST /query/stream` sends Server-Sent Events as the
pipeline crosses the four stage boundaries it already had, and the retrieve event carries the real
number of chunks found. The browser draws the four named stages instantly from its own state, then
fills in "Found 5 official sources" from the wire, and the bar moves only when a stage actually
completes. The finished answer arrives as one payload, already verified. Nothing on screen is
guessed: the source count is real, the elapsed counter is real, and the bar tracks real stages.

## Decisions logged

- `docs/adr/0006-stage-events-not-token-streaming.md` — why token streaming is incompatible with the
  citation gate, and what we do instead.

## Caveats / not done

- **The "Usually 20 to 40 seconds" line was removed in round 3.** It was specified when a query was
  expected to take 20 to 140 seconds; measured reality is p50 5.1 to 7.7s depending on machine load,
  and Phase 8's hosted provider would have moved it again. The waiting state now reads only
  "Answers are written from the sources each time, not recalled from memory." plus the live elapsed
  counter, which stays true regardless of provider. Nothing on screen now states a duration the
  system does not measure.
- **The CLARIFY state renders a literal `--`.** "Could you say a bit more about what you're asking
  -- for example..." comes from `CLARIFY_QUESTION` in `app/guardrails/clarifier.py:141`, which is
  Phase 4 backend copy, not frontend copy. It is a one-word edit but it changes a string the eval
  harness exercises, so I left it rather than touch Phase 4 text inside Phase 6.
- **The no-answer gate only fires for genuinely off-topic questions.** "How do I renew my drivers
  license in Texas?" returns `response_type: answer` with 5 citations, and so do "green card through
  marriage", "H-4 EAD", "consular visa appointment", and "naturalization test". The system stays
  safe because the prompt's rule 3 makes the prose say the sources do not cover it, but the
  structured label is wrong in those cases. That is a `NO_ANSWER_MAX_DISTANCE` calibration question,
  not a Phase 6 one. It is also why the third example chip is a sourdough question: I tested five
  plausible immigration-adjacent alternatives and none of them reliably produces `no_answer`.
- **`docs/design/` is in `.gitignore`.** The prototype this phase was built against will not be
  committed, so the design of record lives only on your disk. Worth deciding deliberately rather
  than by accident.
- **SSE client disconnect.** `event_stream`'s `finally: await task` waits on the pipeline task even
  when the browser has gone away, so a reader who navigates mid-query leaves a generation running to
  completion and can produce a spurious log error. It leaks nothing and affects no gate, but it
  should become `task.cancel()` when the gateway lands in Phase 7.
- **`blocked_unverified`'s mock fixture is not a captured live response.** The other four are real
  captures. The real generator never produced an out-of-range citation across the builder's six
  attempts and my own queries, so that fixture was produced by running the real `answer_question`
  with a fixed-string LLM. Citations, contexts, and freshness in it are real retrieved data, and the
  state's real production is covered by `tests/test_guardrails.py`. Disclosed in `lib/fixtures.ts`.
- **The footer's 1-pixel margin is gone.** Round 4 left the disclaimer fitting on one line by 1px
  (1063px of text in 1064px), which any font fallback could have broken. Round 5 dropped the source
  list from the sentence, since every answer already lists its own sources with dates. It is now 124
  characters needing 705px, leaving **359px of headroom** at every desktop width measured (1440,
  1280, and 1120 all render one line), and it still wraps naturally to four lines at 375px.
- **No dark mode, per the brief.** No login, accounts, history, sidebar, or settings.
- **Not added to `docker-compose.yml`** — CLAUDE.md's compose line lists postgres, orchestrator, and
  otel-lgtm now, plus redis and gateway at Phase 7. The frontend has its own Dockerfile
  (`output: "standalone"`, verified `.next/standalone/server.js` exists) but no compose service.
- **Leftover verification state**: containers `oh-p5-ci`, `oh-p5-fresh`, `oh-ci-verify` are still
  running, and I created an `officehours_ci` fixture database on the postgres container. A local
  `next start` may still be on port 3000. None of it is in the repo.
