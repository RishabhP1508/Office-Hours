// Pure-function tests for lib/freshness.ts. Same style as lib/prose.test.ts and
// lib/sources.test.ts (node:test + node:assert/strict, run by `npm test` -- see package.json's
// `node --test lib/*.test.ts`). This module exists specifically because a `.tsx` component cannot
// be unit tested here without a new test-runner dependency (see lib/freshness.ts's own docstring),
// so every wording decision the Freshness component used to make inline now lives, and is tested,
// here instead.

import { test } from "node:test";
import assert from "node:assert/strict";

import { groupFreshnessNotices, type FreshnessTextPart } from "./freshness.ts";
import type { FreshnessNotice } from "./api.ts";

function makeNotice(overrides: Partial<FreshnessNotice> = {}): FreshnessNotice {
  return {
    source_url: "https://example.gov/a",
    rule_effective_date: null,
    rule_status: "in_force",
    rule_status_source: null,
    rule_status_source_evidences_status: null,
    reason: "top_ranked",
    ...overrides,
  };
}

/** Flattens a parts array back to plain text, for assertions that only care about the prose
 * (the REGRESSION test below, and the CONTROL) rather than the exact link structure. */
function flatten(parts: FreshnessTextPart[]): string {
  return parts.map((p) => (p.kind === "text" ? p.text : p.label)).join("");
}

// --- One test per status, exact wording pinned (deepEqual against the full parts array, the same
// strength `prose.test.ts` pins parseInline's node trees at) ---------------------------------

test("in_force, with date: 'took effect on', the generic source link, and the differs-before-and-after clause", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({ rule_status: "in_force", rule_effective_date: "2026-09-15" }),
  ]);
  assert.deepEqual(group.parts, [
    {
      kind: "text",
      text:
        "A rule affecting this answer took effect on September 15, 2026, so the answer differs " +
        "before and after that date. ",
    },
    { kind: "link", label: "Read the source", url: "https://example.gov/a" },
  ]);
});

test("in_force, with no date at all: states no date, never a fabricated or stale one", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({ rule_status: "in_force", rule_effective_date: null }),
  ]);
  assert.deepEqual(group.parts, [
    { kind: "text", text: "A rule affecting this answer is currently in force. " },
    { kind: "link", label: "Read the source", url: "https://example.gov/a" },
  ]);
  const text = flatten(group.parts);
  assert.equal(text.includes("took effect"), false);
  assert.equal(text.includes("takes effect"), false);
});

test("scheduled: 'takes effect on', the generic source link, and the differs-before-and-after clause", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({ rule_status: "scheduled", rule_effective_date: "2026-09-15" }),
  ]);
  assert.deepEqual(group.parts, [
    {
      kind: "text",
      text:
        "A rule affecting this answer takes effect on September 15, 2026, so the answer differs " +
        "before and after that date. ",
    },
    { kind: "link", label: "Read the source", url: "https://example.gov/a" },
  ]);
});

test("enjoined, evidences=false, with date: maintainer attribution, 'the rule as published' link, no court's-order wording", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({
      rule_status: "enjoined",
      rule_effective_date: "2026-09-15",
      rule_status_source: "https://www.federalregister.gov/d/2026-14439",
      rule_status_source_evidences_status: false,
    }),
  ]);
  assert.deepEqual(group.parts, [
    {
      kind: "text",
      text:
        "A rule affecting this answer was scheduled to take effect on September 15, 2026. It has " +
        "since been blocked by a court order and is not in force. That is recorded by this " +
        "site's maintainer; the page above does not say it. ",
    },
    {
      kind: "link",
      label: "the rule as published",
      url: "https://www.federalregister.gov/d/2026-14439",
    },
  ]);
  const text = flatten(group.parts);
  assert.equal(text.includes("court's order"), false);
});

test("enjoined, evidences=true, with date: no maintainer attribution, 'the court's order' link", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({
      rule_status: "enjoined",
      rule_effective_date: "2026-09-15",
      rule_status_source: "https://www.courtlistener.com/order",
      rule_status_source_evidences_status: true,
    }),
  ]);
  assert.deepEqual(group.parts, [
    {
      kind: "text",
      text:
        "A rule affecting this answer was scheduled to take effect on September 15, 2026. A " +
        "court has blocked it and it is not in force. ",
    },
    { kind: "link", label: "the court's order", url: "https://www.courtlistener.com/order" },
  ]);
  const text = flatten(group.parts);
  assert.equal(text.includes("maintainer"), false);
});

test("enjoined, evidences=false, NO DATE: drops the scheduled-date clause, keeps the maintainer attribution", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({
      rule_status: "enjoined",
      rule_effective_date: null,
      rule_status_source: "https://www.federalregister.gov/d/2026-14439",
      rule_status_source_evidences_status: false,
    }),
  ]);
  assert.deepEqual(group.parts, [
    {
      kind: "text",
      text:
        "A rule affecting this answer has been blocked by a court order and is not in force. " +
        "That is recorded by this site's maintainer; the page above does not say it. ",
    },
    {
      kind: "link",
      label: "the rule as published",
      url: "https://www.federalregister.gov/d/2026-14439",
    },
  ]);
  const text = flatten(group.parts);
  assert.equal(text.includes("scheduled to take effect"), false);
});

test("enjoined, evidences=true, NO DATE: no maintainer attribution, no scheduled-date clause", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({
      rule_status: "enjoined",
      rule_effective_date: null,
      rule_status_source: "https://www.courtlistener.com/order",
      rule_status_source_evidences_status: true,
    }),
  ]);
  assert.deepEqual(group.parts, [
    {
      kind: "text",
      text: "A rule affecting this answer has been blocked by a court order and is not in force. ",
    },
    { kind: "link", label: "the court's order", url: "https://www.courtlistener.com/order" },
  ]);
  const text = flatten(group.parts);
  assert.equal(text.includes("maintainer"), false);
  assert.equal(text.includes("scheduled to take effect"), false);
});

test("not_in_force, evidences=false: maintainer attribution, 'the rule as published' link, never states a date", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({
      rule_status: "not_in_force",
      rule_effective_date: null,
      rule_status_source: "https://www.federalregister.gov/withdrawal-notice",
      rule_status_source_evidences_status: false,
    }),
  ]);
  assert.deepEqual(group.parts, [
    {
      kind: "text",
      text:
        "A rule affecting this answer is not in force. That is recorded by this site's " +
        "maintainer; the page above does not say it. ",
    },
    {
      kind: "link",
      label: "the rule as published",
      url: "https://www.federalregister.gov/withdrawal-notice",
    },
  ]);
});

test("not_in_force, evidences=true: no maintainer attribution, the neutral 'the source for that' link (never 'the court's order')", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({
      rule_status: "not_in_force",
      rule_effective_date: null,
      rule_status_source: "https://www.federalregister.gov/withdrawal-order",
      rule_status_source_evidences_status: true,
    }),
  ]);
  assert.deepEqual(group.parts, [
    { kind: "text", text: "A rule affecting this answer is not in force. " },
    {
      kind: "link",
      label: "the source for that",
      url: "https://www.federalregister.gov/withdrawal-order",
    },
  ]);
  const text = flatten(group.parts);
  assert.equal(text.includes("maintainer"), false);
  assert.equal(text.includes("court's order"), false);
});

test("not_in_force ignores rule_effective_date even when one is present, exactly like the backend", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({
      rule_status: "not_in_force",
      rule_effective_date: "2026-09-15",
      rule_status_source: "https://www.federalregister.gov/withdrawal-notice",
      rule_status_source_evidences_status: false,
    }),
  ]);
  const text = flatten(group.parts);
  assert.equal(text.includes("September 15, 2026"), false);
});

// --- UNKNOWN STATUS: the fallback that stops the next added state repeating this defect --------

test("UNKNOWN STATUS: a rule_status this file does not recognize renders a safe, non-committal notice, never 'takes/took effect'", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({ rule_status: "stayed_pending_appeal", rule_effective_date: "2026-09-15" }),
  ]);
  assert.deepEqual(group.parts, [
    {
      kind: "text",
      text: "This page does not know the status of a rule affecting this answer. ",
    },
    { kind: "link", label: "Read the source", url: "https://example.gov/a" },
  ]);
  const text = flatten(group.parts);
  assert.equal(text.includes("takes effect"), false);
  assert.equal(text.includes("took effect"), false);
});

test("UNKNOWN STATUS also covers a runtime-null rule_status (a legacy/unsynced row), never crashing", () => {
  const notice = makeNotice({
    rule_status: null as unknown as string,
    rule_effective_date: "2026-09-15",
  });
  const [group] = groupFreshnessNotices([notice]);
  const text = flatten(group.parts);
  assert.equal(text.includes("This page does not know the status"), true);
  assert.equal(text.includes("takes effect"), false);
  assert.equal(text.includes("took effect"), false);
});

// --- THE REGRESSION TEST, named for the defect itself ------------------------------------------
//
// Written as an absence check over BOTH phrases, not just the one observed live ("takes effect"),
// so it covers the whole class ("took effect" is exactly as wrong for an enjoined rule, and a
// future fix that flipped the tense without fixing the underlying branch would still pass a
// single-phrase check).

test("REGRESSION (2026-09-19 defect): an enjoined notice's rendered text contains neither 'takes effect' nor 'took effect'", () => {
  const [group] = groupFreshnessNotices([
    makeNotice({
      rule_status: "enjoined",
      rule_effective_date: "2026-09-15",
      rule_status_source: "https://www.federalregister.gov/d/2026-14439",
      rule_status_source_evidences_status: false,
    }),
  ]);
  const text = flatten(group.parts);
  const claimsEffect = text.includes("takes effect") || text.includes("took effect");
  assert.equal(
    claimsEffect,
    false,
    `an enjoined rule's notice must never claim it takes/took effect, got: ${JSON.stringify(text)}`
  );
});

// THE CONTROL for the regression test above: an absence assertion pointed at nothing passes
// forever (see this repo's own recorded lesson on that shape of false confidence). This proves the
// exact check the regression test runs is capable of failing, by running it against the literal
// pre-fix production string this defect actually rendered.
test("CONTROL: the regression test's absence check is not vacuous -- the real pre-fix string trips it", () => {
  const preFixText = "A rule affecting this answer takes effect on September 15, 2026";
  const claimsEffect = preFixText.includes("takes effect") || preFixText.includes("took effect");
  assert.equal(
    claimsEffect,
    true,
    "the pre-fix string must trip the same absence check the regression test runs, or that check is vacuous"
  );
});

// --- Grouping ------------------------------------------------------------------------------

test("grouping: two notices sharing (date, status, source, evidences) collapse into one group with both urls", () => {
  const groups = groupFreshnessNotices([
    makeNotice({
      source_url: "https://studyinthestates.dhs.gov/quick-facts",
      rule_status: "scheduled",
      rule_effective_date: "2026-09-15",
    }),
    makeNotice({
      source_url: "https://studyinthestates.dhs.gov/final-rule-faq",
      rule_status: "scheduled",
      rule_effective_date: "2026-09-15",
    }),
  ]);
  assert.equal(groups.length, 1);
  const linkUrls = groups[0].parts
    .filter((p): p is Extract<FreshnessTextPart, { kind: "link" }> => p.kind === "link")
    .map((p) => p.url);
  assert.deepEqual(linkUrls, [
    "https://studyinthestates.dhs.gov/quick-facts",
    "https://studyinthestates.dhs.gov/final-rule-faq",
  ]);
});

test("grouping: two notices differing only in rule_status do NOT collapse, even sharing a date", () => {
  const groups = groupFreshnessNotices([
    makeNotice({
      source_url: "https://example.gov/a",
      rule_status: "scheduled",
      rule_effective_date: "2026-09-15",
    }),
    makeNotice({
      source_url: "https://example.gov/b",
      rule_status: "enjoined",
      rule_effective_date: "2026-09-15",
      rule_status_source: "https://www.federalregister.gov/d/2026-14439",
      rule_status_source_evidences_status: false,
    }),
  ]);
  assert.equal(groups.length, 2);
  const bothTexts = groups.map((g) => flatten(g.parts));
  assert.ok(bothTexts.some((t) => t.includes("takes effect")));
  assert.ok(bothTexts.some((t) => t.includes("blocked by a court order")));
});

test("grouping: two enjoined notices sharing a date but differing rule_status_source_evidences_status do NOT collapse", () => {
  const groups = groupFreshnessNotices([
    makeNotice({
      source_url: "https://example.gov/a",
      rule_status: "enjoined",
      rule_effective_date: "2026-09-15",
      rule_status_source: "https://www.federalregister.gov/d/2026-14439",
      rule_status_source_evidences_status: false,
    }),
    makeNotice({
      source_url: "https://example.gov/b",
      rule_status: "enjoined",
      rule_effective_date: "2026-09-15",
      rule_status_source: "https://www.courtlistener.com/order",
      rule_status_source_evidences_status: true,
    }),
  ]);
  assert.equal(groups.length, 2);
});

test("groupFreshnessNotices on an empty array returns an empty array", () => {
  assert.deepEqual(groupFreshnessNotices([]), []);
});
