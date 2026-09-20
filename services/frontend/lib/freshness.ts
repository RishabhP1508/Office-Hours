// Pure grouping and sentence construction for the dated/contested-rule notice block
// (components/Freshness.tsx). Extracted here, not left inline in the component, because
// `node --test lib/*.test.ts` (package.json's `npm test`) strips TypeScript types but does not
// compile JSX -- a `.tsx` file cannot be unit tested without a new test-runner dependency, and
// none may be added. `lib/sources.ts`/`lib/sources.test.ts` and `lib/prose.ts`/`lib/prose.test.ts`
// are the established pattern this file follows: pure logic here, the component a thin renderer
// that makes zero copy decisions of its own.
//
// THE DEFECT THIS FILE CLOSES (2026-09-19). The component used to branch on one boolean,
// `notice.in_effect`, which read `False` for `enjoined`, `scheduled`, AND `not_in_force` alike --
// so a rule a federal court had enjoined five days earlier rendered "A rule affecting this answer
// takes effect on September 15, 2026", one paragraph below the correct sentence (in the answer's
// own prose, built by app/guardrails/freshness.py::freshness_notice_text) saying it is not in
// force. `FreshnessNotice.in_effect` is gone from lib/api.ts entirely; every function below reads
// `rule_status` directly, one branch per status, so a status this file does not recognize can
// never fall through into the "took effect"/"takes effect" wording by default -- see
// UNKNOWN_STATUS_STATE below.
//
// WORDING PARITY. The four/five sentence shapes below are deliberately built to carry the exact
// same factual clauses, maintainer attribution, and link labels as
// app/guardrails/freshness.py::freshness_notice_text -- read that function's own docstring before
// changing wording here. The one deliberate difference is the OPENING phrase: the backend's
// sentence is appended inline to the answer's own prose ("One of the sources above describes..."),
// while this notice renders as its own separate visual block (a distinct sand-colored box, not
// running text), so it opens with "A rule affecting this answer..." instead -- adapted framing,
// not a different claim.

import { formatLongDate } from "./format.ts";
import type { FreshnessNotice } from "./api.ts";

const ORDINALS = ["first", "second", "third", "fourth", "fifth"];

export type FreshnessTextPart =
  | { kind: "text"; text: string }
  | { kind: "link"; label: string; url: string };

export interface FreshnessNoticeGroup {
  /** Stable per-group React key: the four fields the group is keyed on, joined. */
  key: string;
  parts: FreshnessTextPart[];
}

/** Joins 1..N URLs as "Read the source" (single) / "the {ordinal} source" (multiple, comma-joined,
 * no "and") -- the pre-existing UI convention for the ordinary in_force/scheduled/unknown case,
 * UNCHANGED by this fix. A bare hostname is never a safe label here: two distinct sources can
 * share one domain (studyinthestates.dhs.gov hosts several of this corpus's fixed-admission
 * pages), which would otherwise print the same label twice. */
function joinGenericSourceLinks(urls: string[]): FreshnessTextPart[] {
  if (urls.length === 1) {
    return [{ kind: "link", label: "Read the source", url: urls[0] }];
  }
  const parts: FreshnessTextPart[] = [];
  urls.forEach((url, i) => {
    if (i > 0) parts.push({ kind: "text", text: ", " });
    parts.push({
      kind: "link",
      label: `the ${ORDINALS[i] ?? "next"} source`,
      url,
    });
  });
  return parts;
}

/** Mirrors app/guardrails/freshness.py::_join_source_links exactly (single: "the {label}";
 * two: "the first {label} and the second {label}"; three+: comma-joined with a trailing
 * ", and") -- used ONLY for the enjoined/not_in_force wordings below, where `label` itself
 * ("rule as published" / "court's order" / "source for that") is a required, must-match string
 * from that backend function's own docstring, not this file's choice. */
function joinStatusSourceLinks(urls: string[], label: string): FreshnessTextPart[] {
  const labelFor = (i: number) =>
    i < ORDINALS.length ? `the ${ORDINALS[i]} ${label}` : `a ${label}`;

  if (urls.length === 1) {
    return [{ kind: "link", label: `the ${label}`, url: urls[0] }];
  }
  const links = urls.map((url, i) => ({ kind: "link" as const, label: labelFor(i), url }));
  if (links.length === 2) {
    return [links[0], { kind: "text", text: " and " }, links[1]];
  }
  const parts: FreshnessTextPart[] = [];
  links.forEach((link, i) => {
    parts.push(link);
    if (i < links.length - 2) parts.push({ kind: "text", text: ", " });
    else if (i === links.length - 2) parts.push({ kind: "text", text: ", and " });
  });
  return parts;
}

// Shared verbatim with app/guardrails/freshness.py::freshness_notice_text's `maintainer_clause` --
// one string, not a phrase copied twice, so the wording cannot drift between the two the next
// time either changes.
const MAINTAINER_CLAUSE =
  "That is recorded by this site's maintainer; the page above does not say it.";

const KNOWN_STATUSES = new Set(["in_force", "scheduled", "enjoined", "not_in_force"]);

interface GroupKey {
  rule_effective_date: string | null;
  rule_status: string | null;
  rule_status_source: string | null;
  rule_status_source_evidences_status: boolean | null;
}

interface RawGroup extends GroupKey {
  urls: string[];
}

/** Collapses notices sharing one (rule_effective_date, rule_status, rule_status_source,
 * rule_status_source_evidences_status) tuple into one group with every source that stated it --
 * NOT keyed on any boolean force bit (that is the exact shape of the removed `in_effect` bug).
 * Keying on all four fields, not just (date, status) the way the backend's own collapsing does,
 * is deliberately NARROWER: two notices sharing a date and status but disagreeing on
 * `rule_status_source`/evidences are a data inconsistency this file refuses to silently merge
 * into one sentence that could only be correct for one of them. */
export function groupFreshnessNotices(notices: FreshnessNotice[]): FreshnessNoticeGroup[] {
  const groups = new Map<string, RawGroup>();
  const order: string[] = [];

  for (const notice of notices) {
    const key: GroupKey = {
      rule_effective_date: notice.rule_effective_date,
      rule_status: notice.rule_status,
      rule_status_source: notice.rule_status_source,
      rule_status_source_evidences_status: notice.rule_status_source_evidences_status,
    };
    const keyString = JSON.stringify(key);
    const existing = groups.get(keyString);
    if (existing) {
      if (!existing.urls.includes(notice.source_url)) existing.urls.push(notice.source_url);
    } else {
      groups.set(keyString, { ...key, urls: [notice.source_url] });
      order.push(keyString);
    }
  }

  return order.map((keyString) => {
    const group = groups.get(keyString)!;
    return { key: keyString, parts: buildParts(group) };
  });
}

function buildParts(group: RawGroup): FreshnessTextPart[] {
  const { rule_effective_date, rule_status, rule_status_source, rule_status_source_evidences_status, urls } =
    group;

  // UNKNOWN STATUS (including a legacy/unsynced row with no `rule_status` at all): never say
  // "takes effect"/"took effect" -- say plainly that this page does not know the status, and link
  // the retrieved source, rather than guessing a force verdict the way the removed `in_effect`
  // bit used to. This is the fallback that stops the next added vocabulary value repeating this
  // defect: a fifth `rule_status` value added to app/rule_status.py without a matching frontend
  // branch lands here, not in a wrong-but-confident sentence.
  if (typeof rule_status !== "string" || !KNOWN_STATUSES.has(rule_status)) {
    return [
      { kind: "text", text: "This page does not know the status of a rule affecting this answer. " },
      ...joinGenericSourceLinks(urls),
    ];
  }

  if (rule_status === "enjoined" || rule_status === "not_in_force") {
    // A legacy/unsynced row can carry `rule_status_source` with no
    // `rule_status_source_evidences_status` at all -- treated as false, the cautious reading,
    // never as true: a link this system cannot confirm actually documents the status must never
    // be captioned as though it does. Mirrors app/guardrails/freshness.py's own comment on this.
    const evidencesStatus = rule_status_source_evidences_status === true;
    // Falls back to the retrieved source urls only if rule_status_source is somehow absent --
    // app/rule_status.py::validate_rule_status requires it for enjoined/not_in_force at load
    // time, so this is a defensive floor against a legacy/unsynced row, never the intended path.
    const statusSourceUrls = rule_status_source ? [rule_status_source] : urls;

    if (rule_status === "enjoined") {
      if (evidencesStatus) {
        const link = joinStatusSourceLinks(statusSourceUrls, "court's order");
        if (rule_effective_date !== null) {
          return [
            {
              kind: "text",
              text:
                `A rule affecting this answer was scheduled to take effect on ` +
                `${formatLongDate(rule_effective_date)}. A court has blocked it and it is not in ` +
                `force. `,
            },
            ...link,
          ];
        }
        return [
          {
            kind: "text",
            text: "A rule affecting this answer has been blocked by a court order and is not in force. ",
          },
          ...link,
        ];
      }
      const link = joinStatusSourceLinks(statusSourceUrls, "rule as published");
      if (rule_effective_date !== null) {
        return [
          {
            kind: "text",
            text:
              `A rule affecting this answer was scheduled to take effect on ` +
              `${formatLongDate(rule_effective_date)}. It has since been blocked by a court order ` +
              `and is not in force. ${MAINTAINER_CLAUSE} `,
          },
          ...link,
        ];
      }
      return [
        {
          kind: "text",
          text:
            `A rule affecting this answer has been blocked by a court order and is not in force. ` +
            `${MAINTAINER_CLAUSE} `,
        },
        ...link,
      ];
    }

    // not_in_force -- deliberately never states rule_effective_date at all (mirrors the backend:
    // that field is not read in this branch), and never names a mechanism (vacated vs.
    // withdrawn), so its evidences=true label stays the neutral "the source for that", never
    // "the court's order" -- see app/rule_status.py's own vocabulary table.
    if (evidencesStatus) {
      const link = joinStatusSourceLinks(statusSourceUrls, "source for that");
      return [
        { kind: "text", text: "A rule affecting this answer is not in force. " },
        ...link,
      ];
    }
    const link = joinStatusSourceLinks(statusSourceUrls, "rule as published");
    return [
      { kind: "text", text: `A rule affecting this answer is not in force. ${MAINTAINER_CLAUSE} ` },
      ...link,
    ];
  }

  // `in_force` or `scheduled` (or a legacy/unsynced row with rule_status null but a date --
  // handled above by the UNKNOWN branch instead, so this point is reached only for a real,
  // recognized status), both wordings mirroring the backend's own "took/takes effect" clause.
  if (rule_effective_date !== null) {
    const verbPhrase = rule_status === "in_force" ? "took effect on" : "takes effect on";
    return [
      {
        kind: "text",
        text:
          `A rule affecting this answer ${verbPhrase} ${formatLongDate(rule_effective_date)}, ` +
          `so the answer differs before and after that date. `,
      },
      ...joinGenericSourceLinks(urls),
    ];
  }

  // `in_force` with no `rule_effective_date` at all -- allowed by app/rule_status.py's own
  // vocabulary table (the date is optional for `in_force`). Unlike the backend (which has
  // `as_of`/today available to state "in force today, {date}"), this component is not passed
  // today's date, so this states only the fact it can state without inventing one: no date claim
  // at all, never a stale or fabricated one.
  return [
    { kind: "text", text: "A rule affecting this answer is currently in force. " },
    ...joinGenericSourceLinks(urls),
  ];
}
