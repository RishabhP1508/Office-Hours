// Pure-function tests for lib/sources.ts. Same style as lib/prose.test.ts (node:test +
// node:assert/strict, run by `npm test` -- see package.json's `node --test lib/*.test.ts`).
// Uses the REAL fixtures from ./fixtures.ts throughout: hand-writing a snippet string here would
// let the test silently pass regardless of whether quoteFromSnippet's breadcrumb rule actually
// fires on real API output.

import { test } from "node:test";
import assert from "node:assert/strict";

import { buildSourceCards, quoteFromChunk, quoteFromSnippet } from "./sources.ts";
import {
  MOCK_ANSWER,
  MOCK_BLOCKED_UNVERIFIED,
  MOCK_REFUSAL_ADVICE,
} from "./fixtures.ts";
import type { AnswerResponse } from "./api.ts";

test("buildSourceCards(MOCK_ANSWER) returns 5 cards, indices 1..5, source_urls in citation order", () => {
  const cards = buildSourceCards(MOCK_ANSWER);
  assert.equal(cards.length, 5);
  assert.deepEqual(
    cards.map((c) => c.index),
    [1, 2, 3, 4, 5]
  );
  assert.deepEqual(
    cards.map((c) => c.url),
    MOCK_ANSWER.citations.map((c) => c.source_url)
  );
  // Pin the actual expected URLs too, not just "matches the citations list" -- this is what
  // catches buildSourceCards silently reading from the wrong field.
  assert.deepEqual(
    cards.map((c) => c.url),
    [
      "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-opt-for-f-1-students",
    ]
  );
});

// Supersedes an earlier test that asserted every card's quote is a substring of
// `citation.snippet`, for ALL cards. That reference changed because the quote's SOURCE changed
// (buildSourceCards now prefers the full chunk body over the 240-char snippet), not because the
// old assertion was inconvenient -- the full body is a strict superset of the snippet, so
// asserting against it is strictly more general, and the property that actually matters (the
// quote is verbatim source text, never fabricated or reordered) is fully covered by
// "verbatim-against-full-chunk property" above. What the snippet-only assertion still legitimately
// covers is the FALLBACK path: when there is no chunk content to draw a quote from, `snippet` is
// all buildSourceCards has, so the verbatim guarantee has to hold there too. This test isolates
// that path by emptying every context's `content` (see the comment further down on why `content`
// alone, not the whole `contexts` array), which forces every card through quoteFromSnippet.
test("verbatim property (snippet fallback path): with context content emptied, every card's quote is a substring of its citation's original snippet", () => {
  let shorterThanSnippetSeen = false;

  for (const response of [MOCK_ANSWER, MOCK_REFUSAL_ADVICE, MOCK_BLOCKED_UNVERIFIED]) {
    // Empties `content` only, not the whole context, and keeps `section_heading` intact.
    // buildSourceCards derives BOTH `fromChunk` (from context.content) and `sectionHeading` (from
    // the same context object) off this array -- emptying `contexts` entirely, as first tried,
    // wipes `sectionHeading` too, and quoteFromSnippet's stripping branch requires a heading to
    // strip. Measured: with `contexts: []`, "shorterThanSnippetSeen" was false for all 15 cards --
    // the stripping branch could never fire, making the instrument control below unsatisfiable by
    // construction, not because stripping is broken. Emptying only `content` forces `fromChunk` to
    // `""` (so every card still falls through to quoteFromSnippet) while leaving `sectionHeading`
    // in place, which is what actually exercises the same stripping path the old, now-superseded
    // test measured.
    const fallbackOnly: AnswerResponse = {
      ...response,
      contexts: response.contexts.map((c) => ({ ...c, content: "" })),
    };
    const cards = buildSourceCards(fallbackOnly);
    assert.equal(cards.length, response.citations.length);

    cards.forEach((card, i) => {
      const snippet = response.citations[i].snippet;
      assert.ok(
        snippet.includes(card.quote),
        `card ${i} quote ${JSON.stringify(card.quote)} is not a substring of its snippet ${JSON.stringify(snippet)}`
      );
      if (card.quote.length < snippet.length) shorterThanSnippetSeen = true;
    });
  }

  // THE INSTRUMENT CONTROL for the property above: without this, the substring assertion passes
  // trivially if quoteFromSnippet's stripping never actually fires (e.g. if it always just
  // returned `snippet.trim()` unchanged). At least one card, across all three fixtures, must have
  // had its breadcrumb actually stripped, proving the substring check above could have failed.
  assert.equal(shorterThanSnippetSeen, true, "no card's quote was ever shorter than its snippet -- the stripping branch never fired");
});

test("chunk 441 (MOCK_ANSWER card 1): quote starts with the eligibility sentence, breadcrumb gone", () => {
  const cards = buildSourceCards(MOCK_ANSWER);
  const card = cards[0];
  assert.equal(MOCK_ANSWER.citations[0].chunk_id, 441);
  assert.ok(card.quote.startsWith("To qualify for the 24-month extension"));
  assert.equal(card.quote.includes(" > "), false);
});

test("chunk 516 (MOCK_REFUSAL_ADVICE): section_heading equals the doc title (no '>' in the breadcrumb), quote starts after it", () => {
  const cards = buildSourceCards(MOCK_REFUSAL_ADVICE);
  const chunk516Index = MOCK_REFUSAL_ADVICE.citations.findIndex((c) => c.chunk_id === 516);
  assert.notEqual(chunk516Index, -1);
  const card = cards[chunk516Index];
  assert.ok(card.quote.startsWith("Last updated: April 28, 2025"));
});

// --- quoteFromChunk -------------------------------------------------------------------------
//
// Chunk 670, copied VERBATIM from the live gateway response to "What is the grace period after
// OPT ends?" on 13 September 2026 (Final Rule: Establishing a Fixed Time Period of Admission and
// an Extension of Stay Procedure FAQ, "Transition Period" group). Its breadcrumb line -- the doc
// title, the group label, and the section heading, which on this FAQ page is an entire question
// -- is 239 characters of the 242-character citation.snippet the API sends for it, which is what
// makes this chunk the regression case: quoteFromSnippet has almost no snippet budget left for
// prose once the breadcrumb is stripped. Do not hand-edit this string; it is real API output, not
// an invented fixture. Note the apostrophe in "rule's" is a right single quote, U+2019.
const REAL_FAQ_CHUNK =
  "Final Rule: Establishing a Fixed Time Period of Admission and an Extension of Stay Procedure FAQ > Transition Period > What happens if I have a current pending application for post-completion OPT or STEM OPT? Do I need to apply for an EOS?\n\n" +
  "Students in the United States admitted under duration of status and present in the United States on Sept. 15, 2026, who timely file for post-completion optional practical training (OPT) or science, technology, engineering and mathematics (STEM) OPT on or before March 18, 2027 (six months after the final rule’s effective date) do not need to apply for an extension of stay (EOS).";

// The section heading the backend would have stored for this chunk -- the breadcrumb's last
// segment, after the final " > ". Used only to drive the instrument control below the same way
// buildSourceCards drives quoteFromSnippet in production.
const REAL_FAQ_HEADING =
  "What happens if I have a current pending application for post-completion OPT or STEM OPT? Do I need to apply for an EOS?";

// Mirrors app/pipeline.py::_snippet exactly (" ".join(content.split()), then slice at 240 and
// back off to the last full word, then "..."), so this reproduces what the API actually sends
// rather than a hand-typed approximation of it.
function apiSnippetFor(content: string, maxLen = 240): string {
  const text = content.trim().split(/\s+/).join(" ");
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen).replace(/\s+\S*$/, "") + "...";
}

// Chunk 669, copied VERBATIM from the live gateway response to "Can I travel outside the US
// while my OPT application is pending?" on 13 September 2026 -- same FAQ page and same
// "Transition Period" group as REAL_FAQ_CHUNK (670) above, but its body is only 184 characters
// after collapsing whitespace, well under the 240-character cut. This is the fixture that proves
// quoteFromChunk's untruncated branch is reachable on real production data: measured across all
// 34 chunks returned by 6 live gateway queries, body lengths ran 184 to 8205 characters, so a
// genuinely short real chunk exists even though none of the three bundled MOCK_* fixtures happens
// to be one. Do not "simplify" this fixture away -- it exists specifically because the bundled
// fixtures cannot exercise this branch.
const REAL_FAQ_CHUNK_669 =
  "Final Rule: Establishing a Fixed Time Period of Admission and an Extension of Stay Procedure FAQ > Transition Period > If I am a current student admitted under duration of status, can I travel after the final rule takes effect?\n\n" +
  "Yes, current F students can continue to travel; however, as of Sept. 15, 2026, upon returning to the United States, these students may be admitted with a new fixed period of admission.";

// Computed independently of quoteFromChunk -- used to build expected values without calling the
// function under test, so tests that compare against it actually check quoteFromChunk's output
// rather than merely checking that it agrees with itself.
function expectedBody(content: string): string {
  const boundary = content.match(/\n\s*\n/);
  const rawBody = boundary && boundary.index !== undefined
    ? content.slice(boundary.index + boundary[0].length)
    : content;
  const withoutImages = rawBody.replace(/!\[[^\]]*\]\([^)]*\)/g, "");
  return withoutImages.split(/\s+/).join(" ").trim();
}

test("quoteFromChunk(REAL_FAQ_CHUNK): reaches the prose the 239-of-242-character breadcrumb blocks quoteFromSnippet from reaching", () => {
  const quote = quoteFromChunk(REAL_FAQ_CHUNK);
  assert.ok(quote.startsWith("Students in the United States admitted under duration of status"));
  assert.equal(quote.includes(" > "), false);
});

test("instrument control for the test above: quoteFromSnippet on the real 242-char API snippet for this chunk cannot reach the prose", () => {
  const snippet = apiSnippetFor(REAL_FAQ_CHUNK);
  // Confirms the measurement this whole fix is built on: the breadcrumb (239 chars) plus "..."
  // (3 chars) is the entire 242-character snippet the API sends for this chunk.
  assert.equal(snippet.length, 242);

  const result = quoteFromSnippet(snippet, REAL_FAQ_HEADING);
  assert.equal(result.includes("Students in the United States"), false);
});

test("verbatim-against-full-chunk property: every real card's quote (minus any trailing '...') is a substring of its chunk's whitespace-collapsed, image-stripped body", () => {
  let sawTruncated = false;

  for (const response of [MOCK_ANSWER, MOCK_REFUSAL_ADVICE, MOCK_BLOCKED_UNVERIFIED]) {
    const cards = buildSourceCards(response);
    cards.forEach((card, i) => {
      const citation = response.citations[i];
      const context = response.contexts.find((c) => c.chunk_id === citation.chunk_id);
      if (!context) return; // no chunk to check the quote against on this card

      const body = expectedBody(context.content);
      const withoutEllipsis = card.quote.endsWith("...")
        ? card.quote.slice(0, -3)
        : card.quote;

      assert.ok(
        body.includes(withoutEllipsis),
        `card for chunk ${citation.chunk_id} quote ${JSON.stringify(card.quote)} is not a substring of its chunk's body ${JSON.stringify(body)}`
      );

      if (card.quote.endsWith("...")) sawTruncated = true;
    });
  }

  // THE INSTRUMENT CONTROL for the truncated half of the property above: without this, the
  // substring assertion would pass trivially if the 240-char cut never actually fired. The
  // untruncated half of this control -- proving the no-truncation branch is also reachable --
  // lives in the dedicated REAL_FAQ_CHUNK_669 test below: none of the 15 cards built from these
  // three bundled fixtures has a body short enough to exercise it (all measured at 716-2754
  // characters), so it needs its own fixture rather than forcing one here.
  assert.equal(sawTruncated, true, "no card's quote was ever truncated with '...' -- the 240-char cut never fired");
});

test("quoteFromChunk(REAL_FAQ_CHUNK_669): a genuinely short real body (184 chars) does not truncate", () => {
  // The untruncated-branch control for the property test above. Measured, not assumed: this
  // chunk's body collapses to 184 characters, under the 240-char cut, so quoteFromChunk must
  // return it whole, with no "..." appended and nothing dropped from the end.
  const quote = quoteFromChunk(REAL_FAQ_CHUNK_669);
  const expected = expectedBody(REAL_FAQ_CHUNK_669);

  assert.equal(expected.length, 184);
  assert.equal(quote, expected);
  assert.ok(quote.startsWith("Yes, current F students can continue to travel"));
  assert.equal(quote.endsWith("..."), false);
});

test("quoteFromChunk strips markdown image syntax (the SEVIS icon), keeping the prose that follows it", () => {
  const content =
    "Optional Practical Training Extension for STEM Students (STEM OPT) > Some Important Notice\n\n" +
    "![Icon - Pay attention to an important point](/sites/default/files/icon/SEVP_SEVIS-HH_Icon_Important.png) " +
    "You must report any change of address to your DSO within 10 days of moving, or you risk a status violation.";

  const quote = quoteFromChunk(content);
  assert.equal(quote.includes("!["), false);
  assert.equal(quote.includes("SEVP_SEVIS-HH_Icon_Important.png"), false);
  assert.ok(quote.includes("You must report any change of address to your DSO"));
});

test("quoteFromChunk leaves bold markdown untouched (the renderer turns ** into <strong> later)", () => {
  const chunk443 = MOCK_ANSWER.contexts.find((c) => c.chunk_id === 443);
  assert.ok(chunk443, "MOCK_ANSWER must still carry chunk 443 for this test to mean anything");
  assert.ok(chunk443!.content.includes("**Student Reporting Responsibilities**"));

  const quote = quoteFromChunk(chunk443!.content);
  assert.ok(quote.includes("**Student Reporting Responsibilities**"));
});

test("quoteFromChunk: no blank line in content returns '' so the caller falls back", () => {
  assert.equal(
    quoteFromChunk("Just one single line, no blank-line separator anywhere in this string at all."),
    ""
  );
});

test("quoteFromChunk: body that is only whitespace returns ''", () => {
  assert.equal(quoteFromChunk("Breadcrumb Title\n\n   \n  \t  "), "");
});

test("quoteFromChunk: body under 40 characters after collapsing returns ''", () => {
  assert.equal(quoteFromChunk("Breadcrumb Title\n\nToo short."), "");
});

test("buildSourceCards: a citation with no matching context still gets a non-empty quote, via the snippet fallback", () => {
  const response: AnswerResponse = {
    answer: "Some answer [1].",
    citations: [
      {
        source_url: "https://example.gov/page",
        chunk_id: 99999,
        snippet: "Some citation snippet text that stands alone with no matching retrieved context entry.",
      },
    ],
    contexts: [],
    disclaimer: "disclaimer",
    generated_at: "2026-09-13T00:00:00Z",
    response_type: "answer",
    refusal_reason: null,
    freshness: null,
  };

  const cards = buildSourceCards(response);
  assert.equal(cards.length, 1);
  assert.notEqual(cards[0].quote, "");
});

// --- Negative controls on quoteFromSnippet directly -----------------------------------------

test("quoteFromSnippet: null sectionHeading returns the snippet unchanged", () => {
  const snippet = "Some page prose that never mentions any heading at all, long enough to matter.";
  assert.equal(quoteFromSnippet(snippet, null), snippet.trim());
});

test("quoteFromSnippet: heading not present in snippet returns the snippet unchanged", () => {
  const snippet = "This snippet talks about OPT eligibility rules but never names the section.";
  assert.equal(quoteFromSnippet(snippet, "A Heading Never Mentioned"), snippet.trim());
});

test("quoteFromSnippet: heading present but not at a breadcrumb boundary returns the snippet unchanged", () => {
  const snippet = "Students must read the Eligibility Rules before filing";
  assert.equal(quoteFromSnippet(snippet, "Eligibility Rules"), snippet.trim());
});

test("quoteFromSnippet: heading at index 0 but fewer than 40 chars follow returns the FULL snippet, not a stub", () => {
  const heading = "Short Heading";
  const snippet = `${heading} only a few words after`;
  const result = quoteFromSnippet(snippet, heading);
  assert.equal(result, snippet.trim());
  assert.notEqual(result, "only a few words after");
});
