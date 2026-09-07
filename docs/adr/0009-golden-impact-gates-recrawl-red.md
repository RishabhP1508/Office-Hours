# 0009: A meaningful change red-lines the recrawl job only when golden rows cite it

## Context

`app/recrawl.py`'s daily refresh (`.github/workflows/recrawl.yml`) already classifies each source's
content as `unchanged`, `cosmetic`, or `meaningful`, and re-indexes only the `meaningful` ones. Until
now `RefreshReport.exit_code()` went red for exactly one reason: a source that failed to fetch. A
`meaningful` change was reported, never gated.

That gap matters because `eval/golden.jsonl` is a fixed file, hand-written by the user, and it is
never read at query time. If USCIS changes a rule the corpus covers, `app/recrawl.py` re-indexes the
new text and the orchestrator starts answering correctly. The next eval run then scores whichever
golden rows cited that page as WRONG, because their `ground_truth_answer` still states the old rule.
Read on its own, that reads as a retrieval regression. It is not one: the system got the new rule
right, and the golden set fell behind it. Nothing before this ADR told anyone that had happened; a
human had to notice a metric dip and go looking for the cause by hand.

Two DHS/USCIS pages in this corpus already have a known future rewrite date: `studyinthestates.dhs.gov`
and `ice.gov` are expected to change on and after Sept 15 2026, when the fixed-period-of-admission
final rule takes effect (ARCHITECTURE.md's "temporal answers" section). That date is not hypothetical
scheduling; it is the case this decision has to hold up against.

## Decision

`app/recrawl.py` now reads `eval/golden.jsonl` once per run, read-only, and for every source whose
verdict is `meaningful`, reports which golden row indices (0-based, matching `golden.jsonl`'s line
order) cite that source, matching on **both** the manifest `source_url` and the post-redirect
`resolved_url` (one manifest entry, `traveling-as-an-international-student`, resolves to
`traveling-as-an-f-or-m-student`, so a golden row could cite either form).

`RefreshReport.exit_code()` now returns 1 on either of two conditions, checked independently:

1. A source is `fetch_failed` (unchanged from before this ADR).
2. A source's verdict is `meaningful` and golden rows cite it, **or** `eval/golden.jsonl` could not
   be read/parsed at all. The second half of that "or" is deliberate: a golden set that cannot be
   checked is never treated as a golden set with nothing affected. A missing or unreadable file
   returns `GoldenImpact(available=False, ...)`, which counts as needing review exactly like a real
   hit does, so a broken read path can never quietly look identical to a clean one.

A `meaningful` change to a source **no** golden row cites stays green: the corpus was correctly
re-indexed and nothing needs a human. Red now carries two distinct meanings, and the run summary says
which one applies, at the top, before anything else, in both `render_table()` and `to_dict()`
(`red_reasons.broken_sources` / `red_reasons.golden_review_sources` in the JSON). Every meaningful
change is still shown in the table on a green run too, with its golden verdict next to it (row
indices, "none cited", or "golden set unavailable") -- the reader needs to see it, they do not need to
be paged about it unless a golden row is actually at stake.

No new service, webhook, or notification provider was added. The GitHub Actions failure email that
already fires on a failed job is the entire delivery mechanism for the new red condition, exactly as
it already was for `fetch_failed`.

## Tradeoff

Red now means two different things, and a reader has to open the run (or read the top of the summary)
to find out which. The alternative, a single meaning for red, was rejected below because the actual
cost of that alternative is a red that fires on days nothing is wrong, and a team that sees that
enough times stops opening the run at all. Two meanings that are each individually clear when surfaced
plainly beat one meaning that stops being trustworthy.

## Alternatives considered

- **Any `meaningful` change goes red, no golden-citation check at all.** Rejected. This corpus has 14
  sources; 8 of them are cited by no golden row today, and those 8 are exactly the ones expected to
  change on Sept 15 2026 (`studyinthestates.dhs.gov`, `ice.gov`). Under this alternative, that expected
  rewrite would fire red the day it lands, and likely again on subsequent days if the rewrite itself
  gets touched up. A human opens the run, finds nothing a golden row needs, and closes it -- and after
  enough of those, starts closing red runs without opening them, which defeats the point of a signal
  that is supposed to mean "look at this."
- **A separate notification channel (Slack webhook, email integration, a bot that opens an issue).**
  Rejected outright, per this project's own scope for this change: build exactly the chosen
  notification and nothing more. The GitHub Actions failure email already exists, requires no new
  credential, and needs no new service to operate or fail on its own.
- **Silently treat an unreadable `golden.jsonl` as zero affected rows.** Rejected. That is
  indistinguishable, from the outside, from "checked, nothing affected" -- the exact failure this
  feature exists to prevent. An unreadable golden set is reported as `available: false` and gates the
  run red, so a broken read path is never mistaken for a clean answer.
