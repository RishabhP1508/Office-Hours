"""Curator-stated rule force: the vocabulary, load-time validation, and the ONE predicate every
consumer (the temporal guard, the freshness notice, the prompt annotation) uses to answer "is this
chunk's rule in force". See docs/adr/0023-curator-rule-status.md for why force is a fact a curator
states, never a fact this codebase derives from arithmetic on a date.

THE BUG THIS CLOSES. Three consumers each inferred "in force" from comparing `rule_effective_date`
to `datetime.now(UTC).date()`, each with its own operator (`>` in the temporal guard, `<=` in the
freshness notice, a third comparison in the prompt annotation). On 2026-09-15, the DHS
fixed-period-of-admission rule's published effective date, all three flipped at once -- except a
federal court had enjoined the rule the day before, so the flip was wrong in all three places at
once, in the same direction, for the same reason: a court can stop a rule without moving the date a
government press release already announced. See REPORT.md, "The injunction, and why this is not
fixed", and docs/adr/0020-temporal-qualification-guard.md's own amendment. Nothing here reads a
date to decide force; a curator states it, in `data/sources/sources.yaml`'s `annotations` block, and
that value is what every consumer reads.

WHY THIS LIVES HERE, NOT UNDER app/guardrails/. Every module under app/guardrails/ that touches a
`RetrievedChunk` already imports app.db and, in freshness.py's case, app.schemas -- real
dependencies that module needs (see freshness.py's own imports). app/prompts.py needs this exact
same enum and predicate to annotate a passage for the model (`_rule_date_note`, formerly keyed on a
date comparison, now keyed on `rule_status`), and app/prompts.py's own docstring already commits to
carrying NO dependency on app.db or app.schemas for a much smaller reason (`_format_date` is
duplicated three times across this codebase rather than imported once, specifically to avoid that
cross-module reach for a three-line saving -- see prompts.py's own comment on its copy). Importing
`app.guardrails.rule_status` from `app.prompts` would pull that whole package's import surface into
a module that has none today, for the sake of putting this file one directory deeper. This module
imports nothing but the standard library, so app/guardrails/* and app/prompts.py can both depend on
it without either acquiring anything new.

THE VOCABULARY (settled with the project owner; do not add a fifth value without updating every
consumer AND the parametrized test in tests/test_guardrails.py that walks all four -- see that
test's own docstring for why it is built to fail loudly on a fifth, untaught state).

    value          means                          in force   rule_effective_date
    in_force       this is the law today          YES        optional; if present, <= today
    scheduled      published, future date,         no         REQUIRED, must be > today
                   nothing blocking it
    enjoined       a court has blocked it;         no         optional, unconstrained vs today
                   may still take effect later
    not_in_force   set aside, vacated, or          no         optional, unconstrained vs today
                   withdrawn; not coming back

`not_in_force` names the CONSEQUENCE (the rule is not the law) rather than the mechanism (vacated
vs. withdrawn vs. superseded): the mechanism is curator prose, `rule_status_source`/
`rule_status_note` in the manifest, never a value this code branches on. Absent `rule_status` AND
absent `rule_effective_date` is an ordinary page -- current law, no notice, no guard, no annotation
-- and that is 12 of the 14 sources in this corpus; their behavior is untouched by any of this.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum


class RuleStatus(StrEnum):
    IN_FORCE = "in_force"
    SCHEDULED = "scheduled"
    ENJOINED = "enjoined"
    NOT_IN_FORCE = "not_in_force"


def is_in_force(rule_status: str | None) -> bool:
    """THE SINGLE PREDICATE. `status == "in_force"`, never a date comparison -- see module
    docstring. Every one of the three consumers (app/guardrails/temporal.py,
    app/guardrails/freshness.py, app/prompts.py) calls this, and only this, to decide whether a
    chunk's rule counts as current; none of them may re-derive the answer from
    `rule_effective_date` on its own.

    `None` -- no `rule_status` annotation at all, the ordinary unannotated page -- reads as in
    force. This is not a default standing in for a missing fact: an unannotated page has no dated or
    contested rule to ask "is this in force" about in the first place, so treating it the same as an
    explicit `in_force` is what lets every consumer fold "current" and "unannotated" into one branch
    (see temporal.py's `_figure_sets`, which does exactly that) instead of carrying a third, special
    case through the whole pipeline for a distinction nothing downstream needs to act on.
    """
    return rule_status is None or rule_status == RuleStatus.IN_FORCE.value


def validate_rule_status(
    *,
    source_url: str,
    rule_status: str | None,
    rule_effective_date: date | None,
    rule_status_source: str | None,
    rule_status_source_evidences_status: bool | None,
    today: date,
) -> None:
    """Raise `ValueError`, naming `source_url` and exactly what is wrong, for every rule this
    module's vocabulary table states. Called at LOAD TIME by every path that turns a curator
    annotation into a stored or graph-state value -- app/ingest.py's two ingest paths,
    app/recrawl.py's `_initial_state`, app/sync_annotations.py's sync loop -- so a curator mistake
    is caught the moment it is entered, not the next time some guard happens to read the corpus and
    silently does the wrong thing with it. `today` is a parameter, never read from the clock in
    here, the same discipline every date-taking function in this codebase already follows.

    THE HARD ERRORS, in the order checked:

    1. `rule_status_source` set with `rule_status_source_evidences_status` absent, or set to
       anything other than a real `bool`. `rule_status_source_evidences_status` declares whether
       the URL in `rule_status_source` actually documents the STATUS (a court's order) or merely
       the rule the status is ABOUT (e.g. the Federal Register notice for the rule itself) -- see
       this module's own docstring on `RuleStatus`. It is never defaulted and never inferred from
       the URL (inferring it would be the exact same guess this whole module exists to stop making,
       relocated rather than removed), so a curator who sets `rule_status_source` without it is
       refused here, the same way a date with no status is refused below. Checked unconditionally,
       before anything below that only fires when `rule_status` itself is set, because a curator
       could in principle set `rule_status_source` on its own.
    2. `rule_effective_date` set with `rule_status` absent. This is the exact bug this module exists
       to prevent from ever being reintroduced: defaulting an unstated status from a date is the
       arithmetic this whole change replaces. Raised regardless of what the date is.
    3. An unrecognized `rule_status` string.
    4. `scheduled` with no `rule_effective_date`, or one that is not strictly after `today`.
       Fail-safe note: this validates that a curator's ENTRY is well-formed at the moment it is
       written or re-synced. It deliberately does NOT protect a `scheduled` row that was valid
       yesterday and whose date has since passed but whose status a curator has not yet updated --
       that is `is_in_force`'s job (a `scheduled` status is never treated as in force, whatever the
       date says), not this function's. This function only runs when an annotation is actually being
       loaded (an ingest, a re-crawl, a sync), so a stale `scheduled` entry raises the next time any
       of those paths touches it, which is the intended nudge to the curator, not a silent runtime
       promotion in either direction.
    5. `in_force` with a `rule_effective_date` that is after `today` -- a curator cannot assert a
       rule is already the law while also dating it into the future.
    6. `enjoined` or `not_in_force` with no `rule_status_source`. This is what keeps the notice
       honest: the system must link the court order or the withdrawal notice, never assert either
       one in its own uncited voice (the exact defect this whole change exists to close -- see
       app/guardrails/freshness.py's docstring on why an appended sentence with no citation is a
       higher bar than ordinary prose).
    """
    if rule_status_source is not None:
        if rule_status_source_evidences_status is None:
            raise ValueError(
                f"{source_url}: rule_status_source is set ({rule_status_source!r}) but "
                "rule_status_source_evidences_status is not. A rule_status_source with no stated "
                "rule_status_source_evidences_status is a hard error -- whether that URL actually "
                "documents the status (the court's order) or merely the rule the status is about "
                "(e.g. the Federal Register notice for the rule itself) must be stated by a "
                "curator, never inferred from the URL."
            )
        if not isinstance(rule_status_source_evidences_status, bool):
            raise ValueError(
                f"{source_url}: rule_status_source_evidences_status must be true or false, got "
                f"{rule_status_source_evidences_status!r} "
                f"({type(rule_status_source_evidences_status).__name__})"
            )

    if rule_status is None:
        if rule_effective_date is not None:
            raise ValueError(
                f"{source_url}: rule_effective_date is set ({rule_effective_date.isoformat()}) "
                "but rule_status is not. A rule_effective_date with no rule_status is a hard "
                "error -- force must be stated by a curator (rule_status), never defaulted from "
                "the date."
            )
        return

    try:
        status = RuleStatus(rule_status)
    except ValueError:
        valid = ", ".join(member.value for member in RuleStatus)
        raise ValueError(
            f"{source_url}: unknown rule_status {rule_status!r} -- must be one of: {valid}"
        ) from None

    if status is RuleStatus.SCHEDULED:
        if rule_effective_date is None:
            raise ValueError(
                f"{source_url}: rule_status is 'scheduled' but rule_effective_date is missing -- "
                "'scheduled' requires a rule_effective_date."
            )
        if rule_effective_date <= today:
            raise ValueError(
                f"{source_url}: rule_status is 'scheduled' but rule_effective_date "
                f"({rule_effective_date.isoformat()}) is not after today "
                f"({today.isoformat()}) -- 'scheduled' requires a future date."
            )
    elif status is RuleStatus.IN_FORCE:
        if rule_effective_date is not None and rule_effective_date > today:
            raise ValueError(
                f"{source_url}: rule_status is 'in_force' but rule_effective_date "
                f"({rule_effective_date.isoformat()}) is after today ({today.isoformat()}) -- "
                "'in_force' requires a date on or before today, or no date at all."
            )

    if status in (RuleStatus.ENJOINED, RuleStatus.NOT_IN_FORCE) and not rule_status_source:
        raise ValueError(
            f"{source_url}: rule_status is {rule_status!r} but rule_status_source is missing -- "
            f"{rule_status!r} requires a citable source for the notice to link."
        )
