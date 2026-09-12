// Pure-function tests for the markdown-rendering additions to prose.ts. There is no
// component-test framework in this project (no jsdom/RTL/vitest dependency, see package.json),
// so per the phase brief these stay unit tests against the pure parser functions rather than
// pulling one in. Run with `npm test` (see package.json), which is `node --test lib/*.test.ts`;
// Node 24's built-in type stripping runs the .ts file directly, no build step, no new dependency.

import { test } from "node:test";
import assert from "node:assert/strict";

import { parseAnswerSegments } from "./citations.ts";
import { parseBlocks, parseInline, type InlineNode } from "./prose.ts";

/** Flattens a node tree's literal text back out, ignoring which markers produced it -- useful for
 * asserting "the asterisks are gone" without hand-writing the whole tree shape. A `citation` node
 * flattens to its bracket form so a straddling bold/italic span can still be asserted against by
 * eye, the same as any other node kind here. */
function flattenText(nodes: InlineNode[]): string {
  return nodes
    .map((node) => {
      if (node.kind === "text") return node.value;
      if (node.kind === "link") return node.label;
      if (node.kind === "citation") return `[${node.indices.join(", ")}]`;
      return flattenText(node.children);
    })
    .join("");
}

test("bold: **30 days** renders bold and the literal asterisks are gone", () => {
  const nodes = parseInline("The period changes from 60 days to **30 days** after completion.");
  assert.deepEqual(nodes, [
    { kind: "text", value: "The period changes from 60 days to " },
    { kind: "bold", children: [{ kind: "text", value: "30 days" }] },
    { kind: "text", value: " after completion." },
  ]);
  const flat = flattenText(nodes);
  assert.equal(flat.includes("**"), false);
  assert.equal(flat, "The period changes from 60 days to 30 days after completion.");
});

test("citation marker next to a markdown link: the citation stays a citation, the link becomes an anchor", () => {
  const text = "See [7] and [the rule](https://example.gov/x) for details.";
  const segments = parseAnswerSegments(text);

  // The citation segment is untouched by markdown parsing -- it was never handed to parseInline.
  const citationSegments = segments.filter((s) => s.kind === "citation");
  assert.deepEqual(citationSegments, [{ kind: "citation", indices: [7] }]);

  // Every text segment, once run through parseInline, must NOT contain a swallowed "[7]" and MUST
  // turn the markdown link into a real link node.
  const textSegments = segments.filter((s): s is { kind: "text"; value: string } => s.kind === "text");
  const allInlineNodes = textSegments.flatMap((s) => parseInline(s.value));

  const linkNodes = allInlineNodes.filter((n) => n.kind === "link");
  assert.equal(linkNodes.length, 1);
  assert.deepEqual(linkNodes[0], {
    kind: "link",
    label: "the rule",
    url: "https://example.gov/x",
  });

  // No inline node anywhere holds a bare citation bracket -- it was already extracted upstream.
  for (const node of allInlineNodes) {
    if (node.kind === "text") {
      assert.equal(/\[7\]/.test(node.value), false);
    }
  }
});

test("a javascript: URL renders as literal text, not an anchor", () => {
  const raw = "[label](javascript:alert(1))";
  const nodes = parseInline(raw);
  assert.deepEqual(nodes, [{ kind: "text", value: raw }]);
  assert.equal(nodes.some((n) => n.kind === "link"), false);
});

test("a data: URL also renders as literal text, not an anchor", () => {
  const raw = "[click](data:text/html,<script>alert(1)</script>)";
  const nodes = parseInline(raw);
  assert.deepEqual(nodes, [{ kind: "text", value: raw }]);
});

test("a bullet list separated by single newlines becomes multiple list items", () => {
  const blocks = parseBlocks("- first point\n- second point\n- third point");
  assert.deepEqual(blocks, [
    {
      kind: "bullet-list",
      items: ["first point", "second point", "third point"],
    },
  ]);
});

test("a numbered list becomes an ordered list", () => {
  const blocks = parseBlocks("1. first step\n2. second step\n3. third step");
  assert.deepEqual(blocks, [
    {
      kind: "numbered-list",
      items: ["first step", "second step", "third step"],
    },
  ]);
});

test("an unmatched ** renders literally", () => {
  const raw = "This has an unmatched marker: **oops, no closing pair.";
  const nodes = parseInline(raw);
  assert.deepEqual(nodes, [{ kind: "text", value: raw }]);
});

test("text with no markdown is unchanged, character for character", () => {
  const raw = "F-1 students on OPT may work up to 20 hours per week during the school year.";
  const nodes = parseInline(raw);
  assert.deepEqual(nodes, [{ kind: "text", value: raw }]);
  assert.equal(flattenText(nodes), raw);
});

test("*italic* and _italic_ both render as italic", () => {
  const starNodes = parseInline("this is *important* text");
  assert.deepEqual(starNodes, [
    { kind: "text", value: "this is " },
    { kind: "italic", children: [{ kind: "text", value: "important" }] },
    { kind: "text", value: " text" },
  ]);

  const underscoreNodes = parseInline("this is _important_ text");
  assert.deepEqual(underscoreNodes, [
    { kind: "text", value: "this is " },
    { kind: "italic", children: [{ kind: "text", value: "important" }] },
    { kind: "text", value: " text" },
  ]);
});

test("bold immediately followed by italic with no separating space parses as two separate nodes", () => {
  const nodes = parseInline("**bold***italic*");
  assert.deepEqual(nodes, [
    { kind: "bold", children: [{ kind: "text", value: "bold" }] },
    { kind: "italic", children: [{ kind: "text", value: "italic" }] },
  ]);
});

test("a paragraph that mixes a heading line, a bullet list, and plain text splits into separate blocks", () => {
  const blocks = parseBlocks("### Deadlines\n- file by day 60\n- file by day 30\nCheck with your DSO.");
  assert.deepEqual(blocks, [
    { kind: "heading", text: "Deadlines" },
    { kind: "bullet-list", items: ["file by day 60", "file by day 30"] },
    { kind: "paragraph", text: "Check with your DSO." },
  ]);
});

test("a plain paragraph with no list or heading lines stays one paragraph block", () => {
  const blocks = parseBlocks("Line one of the paragraph.\nLine two of the same paragraph.");
  assert.deepEqual(blocks, [
    { kind: "paragraph", text: "Line one of the paragraph. Line two of the same paragraph." },
  ]);
});

// --- Single-pass tokeniser: a markdown span containing a citation marker --------------------
//
// These are regression tests for the defect the old two-pass pipeline had: citation parsing ran
// over the whole string first, so a bold/italic span whose opening and closing markers straddled
// a citation marker got split into two text segments, and neither marker half ever matched its
// partner. `parseInline` now recognizes citation markers as one more token in the same scan as
// bold/italic/link, so a span can legitimately contain a citation.

test("bold span containing a citation: **30 days [7]** is one bold node with a nested citation, no literal asterisk survives", () => {
  const nodes = parseInline("The new rule caps it at **30 days [7]** after your program ends.");
  assert.deepEqual(nodes, [
    { kind: "text", value: "The new rule caps it at " },
    {
      kind: "bold",
      children: [
        { kind: "text", value: "30 days " },
        { kind: "citation", indices: [7] },
      ],
    },
    { kind: "text", value: " after your program ends." },
  ]);
  const flat = flattenText(nodes);
  assert.equal(flat.includes("*"), false);
});

test("italic span containing a citation (the real production example): *...extension [1].* is one italic node with a nested citation, no literal asterisk survives", () => {
  const raw =
    "*A higher-level STEM degree earned later can give you one additional 24-month extension [1].*";
  const nodes = parseInline(raw);
  assert.deepEqual(nodes, [
    {
      kind: "italic",
      children: [
        {
          kind: "text",
          value:
            "A higher-level STEM degree earned later can give you one additional 24-month extension ",
        },
        { kind: "citation", indices: [1] },
        { kind: "text", value: "." },
      ],
    },
  ]);
  const flat = flattenText(nodes);
  assert.equal(flat.includes("*"), false);
});

test("a bare [7] still renders as a citation, not as plain text and not as a link", () => {
  const nodes = parseInline("[7]");
  assert.deepEqual(nodes, [{ kind: "citation", indices: [7] }]);
});

test("a markdown link and an adjacent citation marker each keep their own identity", () => {
  const nodes = parseInline("[the rule](https://example.gov/x)[7]");
  assert.deepEqual(nodes, [
    { kind: "link", label: "the rule", url: "https://example.gov/x" },
    { kind: "citation", indices: [7] },
  ]);
});

test("a bold span containing two adjacent citation markers keeps both as separate citation nodes", () => {
  const nodes = parseInline("**Confirmed by two sources [1][2].**");
  assert.deepEqual(nodes, [
    {
      kind: "bold",
      children: [
        { kind: "text", value: "Confirmed by two sources " },
        { kind: "citation", indices: [1] },
        { kind: "citation", indices: [2] },
        { kind: "text", value: "." },
      ],
    },
  ]);
  const flat = flattenText(nodes);
  assert.equal(flat.includes("*"), false);
});
