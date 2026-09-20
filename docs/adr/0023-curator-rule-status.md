# 0023: `rule_status` is stated by a curator; force is never derived from a date

## Context

A federal court can stop a rule without moving the date the agency published for it. On
2026-09-14, a nationwide preliminary injunction blocked the DHS fixed-period-of-admission final
rule the day before its own published effective date of 2026-09-15. Three places in this codebase
each answered "is this rule in force" by comparing `rule_effective_date` to `datetime.now(UTC)
.date()`, each with its own operator, and all three flipped at the same moment, in the same wrong
direction, for the same reason:

- `app/guardrails/temporal.py::_figure_sets` gated on `rule_effective_date > today`. At
  `2026-09-15`, that comparison went false, the guard's `future_only` figure set emptied, and both
  the BLOCK and the INSERT paths went silent -- on the one day silence mattered most.
- `app/guardrails/freshness.py::build_freshness` computed `in_effect = rule_effective_date <=
  today`. At `2026-09-15`, `in_effect` flipped to `True`, and `freshness_notice_text` rendered "took
  effect on September 15, 2026" -- asserted in this system's own uncited voice, about a rule a
  federal court had enjoined the day before.
- `app/prompts.py::_rule_date_note` used the same `<=` comparison to choose between "took effect
  on" and "takes effect on". The model was told the rule had already taken effect, and prompt rule
  4's own trigger (anchored on the literal phrase "takes effect on" appearing on some passage)
  stopped firing, because no passage carried that phrase any longer.

See REPORT.md, "The injunction, and why this is not fixed", for the day-by-day account, and
docs/adr/0020-temporal-qualification-guard.md's own amendment, which recorded the first two of
these three as they were discovered. Nothing was fixed at the time: the site was not public, and
the two fast options -- editing `rule_effective_date` to a date DHS never published, or curating a
citable source for a court order on the night it issued -- were both judged worse than waiting.
This ADR is that wait's outcome: a third option, a field that does not depend on a date comparison
at all.

## Decision

`rule_status` is a new column on `documents`, one of four values, STATED by a curator in
`data/sources/sources.yaml`'s `annotations` block, never computed from `rule_effective_date` or
from the clock:

| value | means | in force | `rule_effective_date` |
|---|---|---|---|
| `in_force` | this is the law today | YES | optional; if present, must be <= today |
| `scheduled` | published, future effective date, nothing blocking | no | REQUIRED, must be > today |
| `enjoined` | a court has blocked it; may still take effect later | no | optional, unconstrained vs today |
| `not_in_force` | set aside, vacated, or withdrawn; not coming back | no | optional, unconstrained vs today |

`rule_effective_date` keeps its original, narrower meaning -- the date the agency published, a fact
about the rule's own history -- and stops being an input to any force decision anywhere in this
codebase. `app/rule_status.py::is_in_force` is the ONE predicate every consumer calls to answer "is
this chunk's rule in force": `status == "in_force"` (or no `rule_status` at all -- see below),
never a date comparison. It lives in `app/rule_status.py`, not under `app/guardrails/`, because
`app/prompts.py` needs the identical enum and predicate and deliberately carries no dependency on
`app.db`/`app.schemas` today (see that module's own docstring on why `_format_date` is duplicated
three times rather than imported once); putting the shared vocabulary somewhere with zero
dependencies of its own lets every consumer -- guardrail or prompt template -- import it without
acquiring a dependency it does not already have.

### Why four states, and why not more

**`stayed pending appeal` was considered and rejected as a fifth value.** A stay and an injunction
both stop a rule from operating right now, and this codebase's own predicate treats them
identically: `is_in_force` only ever asks "in force, yes or no", never "by what mechanism is it
not". Splitting `enjoined` from `stayed` would add a value with no distinct code path behind it --
every consumer would branch on it exactly the way it branches on `enjoined` today. The mechanism
belongs in `rule_status_note`, free text no code reads, precisely so a future case with a genuinely
new mechanism (a stay, a remand, a partial vacatur) does not need a new enum value and four
consumers taught about it; it needs a sentence a human reads.

**`proposed` (a rule not yet final, still in notice-and-comment) was considered and rejected.** A
proposed rule is not a rule this project cites at all today -- the corpus holds only final rules and
their FAQs -- and adding a status for a document type nothing currently ingests would be
speculative coverage for a case this corpus does not have, the same discipline
`app/guardrails/temporal.py`'s own docstring already applies to `_NUMERIC_DATE_RE` (built for a
footer format measured in the corpus, not one merely imaginable).

### Why `not_in_force` names the consequence, not the mechanism

A rule can stop being law by being vacated by a court, withdrawn by the agency, or superseded by a
later rule -- three different legal mechanisms with one identical consequence for this system: the
rule is not in force, and it is not coming back on its own. `not_in_force` names that consequence.
The mechanism is exactly the kind of nuance `rule_status_note` exists for (free text, no code reads
it) rather than a second axis of enum values, because nothing downstream of "is this in force"
needs to distinguish a vacated rule from a withdrawn one -- both render the identical notice and
trigger the identical guard behavior. Naming the value after the mechanism instead (`vacated`,
`withdrawn`, `superseded`) would have made three consumers each need a three-way branch that always
produces the same two-way answer.

### Why a date with no status is a hard error, not a default

The bug this ADR closes was three places independently defaulting "is this in force" from a date.
The only way to make that class of bug structurally impossible to reintroduce is to make the
combination that produces it -- a date with no stated force -- impossible to load at all.
`app/rule_status.py::validate_rule_status` raises, naming the `source_url` and exactly what is
wrong, the moment a curator annotation with `rule_effective_date` set and `rule_status` absent is
read from the manifest (or, for the CI fixture corpus, from a snapshot's own frontmatter -- see
`app/ingest.py::_ingest_from_snapshots`'s own docstring for why that path reads annotations
differently but is validated identically). This is deliberately a LOAD-TIME check, run in every
path that turns a curator annotation into a stored or graph-state value (`app/ingest.py`'s two
ingest paths, `app/recrawl.py::_initial_state`, `app/sync_annotations.py`'s sync loop) rather than a
runtime check in the guards themselves: a curator finds out immediately, from the tool they just
ran, rather than the next time some guard happens to read the corpus and silently does the wrong
thing with an unstated combination.

Absent `rule_status` AND absent `rule_effective_date` together is not an error at all -- it is the
ordinary, unannotated page, 12 of this corpus's 14 sources, and their behavior is untouched by any
of this: `is_in_force(None)` reads `True`, so an unannotated chunk's figures fold into the same
CURRENT set an explicit `in_force` chunk's figures do (see `app/guardrails/temporal.py::
_figure_sets`), and `build_freshness`/`format_context` never even build a notice or a note for a
chunk carrying neither annotation.

### Why `rule_status_source` is required for `enjoined`/`not_in_force`

The defect this ADR is most directly a response to was not merely a wrong date comparison; it was
this system asserting, in a rendered sentence with no citation, that a rule enjoined by a federal
court had "taken effect". `rule_status_source` -- a URL, required by `validate_rule_status` whenever
`rule_status` is `enjoined` or `not_in_force` -- is what keeps the notice honest going forward: an
`enjoined`/`not_in_force` sentence links the court order or the withdrawal notice
(`app/guardrails/freshness.py::freshness_notice_text`) instead of asserting the block or the
withdrawal in this system's own uncited voice. `in_force` and `scheduled` need no such field,
because the source page itself is the citation for "this rule exists and takes/took effect on this
date" -- there is nothing contested to separately source.

**The real `rule_status_source` curated for this project's own two `fixed_admission` entries points
at the Federal Register document for the rule itself, not at the court's order.** No citable URL
for the order was in this repository when this change was made, and inventing a PACER or
CourtListener link was out of scope for it. This is flagged as a curator decision, not resolved
here: `data/sources/sources.yaml`'s own comment on both entries says so, and a better source (a
direct citation to the order, or a court-reporting summary of it) is expected to replace it later.
The honest, incomplete citation was judged better than fabricating one or leaving the field
required-but-absent, which `validate_rule_status` would have refused to load at all.

**19 September 2026: `rule_status_source_evidences_status` closes the gap the paragraph above
describes, rather than leaving it as a permanent caveat.** A `rule_status_source` URL answers "is
there a citable link at all", never "does that link actually say what the rendered sentence says".
This project's own two `fixed_admission` entries prove the difference is real: the curated
`rule_status_source` points at the Federal Register notice for the rule itself, not at the court's
order, so a notice that treated "has a `rule_status_source`" as "is evidenced" would caption that
link as though the linked page said the rule was blocked, when the page says no such thing. A
second boolean, not an inference from the URL, is the fix: inferring it (a domain allowlist, a
regex for "court" or "order" in the URL) would be the exact same guess this whole ADR exists to stop
making, merely relocated from a date comparison to a string comparison. `validate_rule_status`
requires it whenever `rule_status_source` is set (never defaulted, never optional-with-a-fallback),
so a curator states, for every citable source, whether that source actually documents the STATUS or
merely the rule the status is ABOUT. `app/guardrails/freshness.py::freshness_notice_text` branches
its enjoined/not_in_force wording on this field: when the source does not evidence the status, the
force claim is attributed to "this site's maintainer" rather than voiced as fact and the reader is
told outright that the linked page does not say it; when the source does evidence the status, the
sentence drops that attribution and captions the link as what it actually is (the court's order for
`enjoined`, or the neutral "the source for that" for `not_in_force`, which -- unlike `enjoined` --
never names a mechanism at all; see this ADR's own "Why `not_in_force` names the consequence, not
the mechanism" above).

## Consumer behavior

Exact wording and the block-vs-insert axis are recorded in each consumer's own docstring
(`app/guardrails/temporal.py`, `app/guardrails/freshness.py`, `app/prompts.py`) rather than
duplicated here, since the code is what actually has to stay accurate; this ADR records why the
vocabulary is shaped the way it is, not a second copy of what each function prints. One property
worth stating in one place because it spans all three: `enjoined` is not made "always block" in the
temporal guard just because it is the injunction's own status. The BLOCK-vs-INSERT axis is, and
stays, "does the firing sentence also name an in-force figure" -- exactly the same test a
`scheduled` figure is held to. An `enjoined` figure asserted alone, as current, is exactly as false
as a `scheduled` one asserted the same way; an `enjoined` figure asserted alongside its current
counterpart is exactly as fixable by inserting a qualifier. Status changes which SENTENCE gets
printed, never which of the two actions fires.

## Tradeoff

Every consumer now depends on a curator actually setting `rule_status` correctly, rather than the
system deriving something (even if it derived it wrong) on its own. A curator who adds a
`rule_effective_date` and forgets `rule_status` gets a loud, immediate error instead of a silently
wrong answer -- which is the entire point -- but it does mean a rule change now requires a person to
make an explicit judgment call about force, rather than the system inferring one from a date that
turned out not to be reliable. That is accepted: the alternative is the bug this ADR exists to
close.

**The `scheduled` time bomb is accepted deliberately, not missed.** A `scheduled` entry is only
valid, per `validate_rule_status`, while its `rule_effective_date` is strictly after `today` (see
that function's own rule 4 above). The moment that date passes without a curator updating
`rule_status` to `in_force` (or to whatever it should become), the SAME entry that was valid
yesterday hard-errors today -- and because `validate_rule_status` runs at load time, in every
ingest, re-crawl, and sync path, that one stale entry stops the ENTIRE corpus from ingesting, with
no warning beforehand: nothing tells a curator in advance that a `scheduled` row is about to expire.
This is FAIL-CLOSED BY DESIGN, not an oversight to be smoothed over later: a hard stop that pages a
human is the outcome this ADR prefers to the alternative, which is the system quietly asserting a
contested or blocked rule is in force because a curator has not yet gotten to it -- precisely the
failure of 15 September 2026 this whole ADR exists to close (see this ADR's own Context section
above, and REPORT.md's "The injunction, and why this is not fixed"). Whoever hits this error should
read it as the fail-safe working, not as a bug: it fires on a DATE ROLLOVER, not on any code change,
so it will land at an inconvenient moment -- a weekend, a holiday, the middle of an unrelated
deploy -- and the fix is a one-line manifest edit (update `rule_status`, run `python -m
app.sync_annotations` or wait for the next scheduled re-crawl), never a code change to relax the
check itself.

## Alternatives considered

**Keep deriving force from `rule_effective_date`, and add a second date (`rule_blocked_at`) a
curator sets when a court intervenes.** Rejected: this still leaves three consumers independently
comparing two dates against `today` with their own operators, which is the exact shape of bug this
ADR closes, merely with one more date to get the comparison wrong on.

**A single boolean (`in_force: bool`) instead of a four-value enum.** Rejected: a boolean cannot
distinguish `scheduled` (not yet in force, but will be, on a known date, unless something
intervenes) from `enjoined` (not in force, might still become so later, no known date) from
`not_in_force` (not in force, not coming back) -- and those three produce three different sentences
in `app/prompts.py` and `app/guardrails/freshness.py`, not one.

**Compute `rule_status` automatically from a curated list of known injunctions/court dockets.**
Rejected as out of scope and over-engineered for a corpus of 14 sources with, to date, exactly one
rule ever contested: building and maintaining a feed of court dockets is a materially larger system
than the four-value field it would replace, for a problem this project's curator can solve by
reading the news and editing one line of YAML.

## Update, 2026-09-19: `in_effect` removed

`app/schemas.py::FreshnessNotice.in_effect` and `services/frontend/lib/api.ts`'s matching field are
both gone. That boolean was originally kept "for compatibility" so the frontend would not need
this ADR's four-value vocabulary at all, but a boolean cannot carry the vocabulary's own
distinction any better on the frontend than it could have on the backend: `is_in_force` reads
`enjoined`, `scheduled`, and `not_in_force` as the identical `False`, and the frontend, reading
only that one bit, rendered "A rule affecting this answer takes effect on September 15, 2026" for
a rule a federal court had enjoined five days earlier. The compatibility field did not avoid this
ADR's defect; it re-imported it onto a second surface. `services/frontend/lib/freshness.ts` now
reads `rule_status` directly, one branch per value, with the same four-state vocabulary this ADR
defines and the same "unrecognized value renders safely, never as a force claim" discipline
`app/rule_status.py::is_in_force` was built to enforce on the backend.
