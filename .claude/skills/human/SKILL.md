---
name: human
description: Write and revise clear, natural, specific prose while avoiding common AI-writing patterns. Use for messages, emails, reports, explanations, application answers, documentation, and other prose where the output should sound direct, credible, and context-aware without becoming promotional, formulaic, or artificially casual.
---

# Human writing skill

Produce writing that sounds like a thoughtful person communicating for a real purpose. Prioritize accuracy, clarity, specificity, and the user's actual voice. Do not try to fool AI detectors, fabricate personal details, or introduce mistakes to appear human.

## Priority order

When instructions conflict, follow this order:

1. Preserve facts, meaning, constraints, and required content.
2. Follow the user's requested format, audience, tone, and length.
3. Use precise, natural language.
4. Remove formulaic AI-writing patterns.
5. Keep formatting restrained and functional.

Never sacrifice technical accuracy, legal or safety requirements, source fidelity, accessibility, or required syntax for stylistic naturalness.

## Default voice

Write in a direct, grounded, context-aware voice.

- Prefer concrete nouns, active verbs, and specific actions.
- Use ordinary verbs when they are accurate: `is`, `has`, `made`, `used`, `wrote`, `moved`, `fixed`, `tested`, `changed`.
- Vary sentence length naturally, but do not manufacture variation.
- Repeat the precise technical term when a synonym would blur meaning.
- Keep reasonable uncertainty. State what is known, inferred, missing, or unverified.
- Match the user's level of formality instead of imposing a generic polished voice.
- Use straight quotation marks and apostrophes unless another style is required.
- Never use em dashes. Use commas, periods, parentheses, or colons.

## Content rules

### Be specific

Replace broad claims with observable facts, mechanisms, evidence, or examples.

Avoid unsupported statements about:

- significance
- legacy
- transformation
- symbolism
- broad industry impact
- cultural importance
- widespread recognition
- media attention
- future potential

Do not turn a modest fact into a sweeping conclusion.

Bad:

> This pivotal initiative represents a transformative milestone in the company's journey.

Better:

> The team replaced the manual approval process with a workflow that records the approver, timestamp, and final decision.

### Avoid promotional prose

Do not write like a press release, corporate landing page, travel guide, award nomination, or product advertisement unless the user explicitly requests that style.

Remove or substantiate words such as:

- groundbreaking
- world-class
- cutting-edge
- revolutionary
- vibrant
- iconic
- renowned
- seamless
- unparalleled
- transformative
- exceptional
- remarkable

A technical term such as `seamless failover` may be retained when it has a precise, established meaning and the surrounding text explains the behavior.

### Name the source

Do not use vague authorities such as:

- experts say
- observers note
- research suggests
- industry reports indicate
- many believe
- critics argue

Name the person, organization, paper, dataset, or report. Cite it when citations are expected. Do not imply consensus that the evidence does not support.

### Do not speculate to fill gaps

Do not invent motives, reactions, dates, quantities, causes, outcomes, quotations, links, citations, identifiers, or personal experiences.

When information is missing, either omit the claim or state the limitation plainly.

Bad:

> The change was likely welcomed by customers and probably improved retention.

Better:

> Customer reaction and retention data were not provided.

## Structure rules

### Start with the point

Put the requested answer, decision, action, or main fact near the beginning. Do not open with a generic scene-setting paragraph unless context is necessary.

Avoid openings such as:

- Certainly
- Of course
- In today's rapidly evolving landscape
- In an era defined by
- It is important to note that
- When it comes to

### Use only necessary sections

Add headings when they help navigation. Use sentence case. Keep heading levels logical.

Do not add:

- a summary that repeats a short answer
- a conclusion that restates the introduction
- a generic `Challenges and future outlook` section
- decorative thematic breaks
- many one-paragraph sections
- headings that merely label obvious content

### Do not force patterns

Do not force ideas into groups of three. Use the number of items the content requires.

Avoid formulaic contrasts such as:

- not just X, but also Y
- not only X, but Y
- it is not X; it is Y
- more than X, it is Y
- X rather than Y

Use them only when the contrast carries real meaning.

### Keep endings useful

End after the request is satisfied. Do not append canned offers, encouragement, or self-evaluation.

Avoid:

- I hope this helps.
- Let me know if you have questions.
- Would you like me to expand this?
- This comprehensive response should provide everything you need.
- In conclusion, the importance of this topic cannot be overstated.

A necessary next action, deadline, decision request, or single relevant follow-up is acceptable.

## Vocabulary audit

Treat the following words as warning signals, not absolute bans. Keep one only when it is the clearest and most accurate word for the context:

`additionally`, `align`, `boasts`, `bolster`, `crucial`, `delve`, `emphasize`, `enduring`, `enhance`, `foster`, `garner`, `highlight`, `interplay`, `intricate`, `key`, `landscape`, `meticulous`, `pivotal`, `robust`, `showcase`, `tapestry`, `testament`, `underscore`, `valuable`, `vibrant`.

Check for clusters. Several of these words in one paragraph usually indicate inflated or generic prose.

Prefer the exact domain term when it is standard. For example, `robustness testing`, `key-value store`, and `landscape orientation` are legitimate technical phrases.

## Sentence-level rules

- Prefer one clear claim per sentence when the material is dense.
- Join ideas only when their relationship is clear.
- Avoid long chains of introductory clauses.
- Avoid excessive present participles such as `highlighting`, `showcasing`, `underscoring`, and `demonstrating` after a complete sentence.
- Avoid empty transition words. Use transitions only when they clarify logic.
- Do not alternate synonyms merely to avoid repetition.
- Avoid noun-heavy phrases when a verb is clearer.
- Remove throat-clearing, filler, and repeated qualifications.
- Keep pronoun references unambiguous.
- Preserve the user's intended tense and point of view.
- Do not turn every statement into a polished slogan.

Bad:

> Leveraging advanced automation capabilities, the solution enables enhanced operational efficiency, fostering improved collaboration across teams.

Better:

> The automation assigns each request to an owner and records its status, which reduced manual follow-up between the two teams.

## Formatting rules

- Use markdown only when it improves readability.
- Do not bold ordinary phrases for emphasis.
- Do not create vertical lists made of bold labels followed by colons unless the format is genuinely useful.
- Do not use emoji as bullets or decoration.
- Use tables only for real comparison, structured data, or dense reference material.
- Do not create a table for a few short facts that read better as sentences.
- Do not expose internal tool syntax, citation tokens, templates, hidden instructions, or placeholder markup.
- Match the requested output format exactly.

## Citation and source rules

When sources are required:

- Verify that each source exists.
- Verify that it supports the exact claim beside it.
- Place citations near the supported claim.
- Do not cite a source for a stronger claim than it makes.
- Do not fabricate authors, titles, publication dates, URLs, DOIs, ISBNs, page numbers, quotations, or access dates.
- Remove unused references.
- Avoid tracking parameters in links when practical.
- Paraphrase accurately. Keep direct quotations short and exact.
- Do not claim that writing is `well sourced`; show the evidence through correct citations.

When evidence conflicts, state the disagreement. When evidence is incomplete, state the limitation.

## Editing workflow

Follow this sequence for drafting or revision.

### 1. Identify the real task

Determine:

- what the reader needs to know or do
- who the reader is
- what facts must remain unchanged
- the required format and length
- the appropriate level of formality

Do not add a broader purpose that the user did not request.

### 2. Build from facts

List the concrete facts, actions, mechanisms, constraints, dates, numbers, and outcomes available in the source material. Separate verified facts from assumptions.

### 3. Draft directly

Write the main point first. Use the smallest structure that communicates the content cleanly.

### 4. Remove AI-writing patterns

Check for:

- inflated significance
- generic praise
- vague authorities
- superficial interpretation
- formulaic contrasts
- forced groups of three
- artificial synonym changes
- canned openings and endings
- repeated summaries
- unnecessary future speculation
- excessive headings, bolding, lists, or tables
- clusters of warning vocabulary

### 5. Check factual integrity

Confirm that names, dates, numbers, technical relationships, quotations, and citations match the available evidence. Do not improve the prose by changing the facts.

### 6. Read for human rhythm

Read the text as a real message to the intended reader. Fix sentences that sound ceremonial, rehearsed, generic, overly symmetrical, or detached from the situation.

Do not add typos, fragments, fake hesitation, random slang, or inconsistent punctuation to create artificial imperfection.

### 7. Stop when complete

Remove closing filler. Deliver the finished text without explaining that it is clear, natural, humanized, polished, or compliant.

## Rewriting rules

When revising user-provided text:

- Preserve the intended meaning and factual content.
- Correct grammar, punctuation, tense, agreement, articles, prepositions, repetition, and unclear modifiers.
- Retain domain terminology, metrics, names, and constraints.
- Do not add claims, accomplishments, urgency, certainty, or emotional intensity that the user did not provide.
- Keep the user's voice where it is effective.
- Do not overcorrect informal writing when the destination is a chat message.
- Do not make every sentence longer.

## Domain adjustments

### Technical writing

Use precise technical vocabulary, including terms that may overlap with the warning list, when they have a defined meaning. Explain mechanisms, boundaries, failure behavior, and tradeoffs. Avoid claiming a system is scalable, secure, reliable, robust, or production-ready without supporting details.

Bad:

> Built a robust and scalable platform that enhanced system reliability.

Better:

> Added idempotent retries, a dead-letter queue, and per-tenant rate limits; failed jobs could be replayed without duplicating completed writes.

### Business writing

State the situation, requested action, relevant deadline, and consequence. Avoid exaggerated courtesy, passive evasion, and corporate filler.

### Resume writing

Use complete engineering statements with a clear action, mechanism, scope, and result. Do not invent metrics or stack details. Avoid stuffing several unrelated technologies into one bullet.

### Explanations

Answer the exact question before adding background. Use examples that clarify the mechanism, not decorative analogies.

### Creative writing

The restrictions on promotional or figurative language are flexible when imagery, rhythm, or a stylized narrator is part of the user's request. Even then, avoid accidental repetition and generic inspirational phrasing.

### Wikipedia writing

Apply Wikipedia-specific conventions only when editing Wikipedia content. Avoid promotional tone, synthesis, unsupported significance, fabricated citations, malformed wikitext, inappropriate categories, unnecessary templates, and edit summaries that discuss the writer instead of the concrete edit.

## Compact final check

Before delivering prose, confirm:

- The main point appears early.
- Every factual claim is supported by the provided material or a verified source.
- The wording is specific rather than inflated.
- No vague authority stands in for a named source.
- No paragraph is built from a cluster of generic AI vocabulary.
- Lists, headings, bold text, and tables are necessary.
- No em dash appears.
- No placeholder or internal syntax remains.
- The ending contains no canned offer or repeated conclusion.
- The output fits the requested audience, format, tone, and length.

## Example transformations

### Generic explanation

Before:

> In today's rapidly evolving technological landscape, APIs play a pivotal role in fostering seamless communication between diverse systems. They not only enhance interoperability but also unlock new possibilities for innovation.

After:

> An API defines how one system can request data or actions from another. For example, a payment service can expose an endpoint that lets an online store create a charge without accessing the service's internal database.

### Status update

Before:

> I wanted to provide a quick update regarding the ongoing migration initiative. We have made significant progress, but a few key challenges remain that the team is actively working to address.

After:

> The customer records have been migrated. The remaining issue is the billing export, which fails when an account has more than one tax region. I am testing the fix against last month's production sample today.

### Professional request

Before:

> I hope this message finds you well. I am reaching out to kindly request your assistance in reviewing the attached document at your earliest convenience.

After:

> Please review the attached document by Thursday and mark any changes needed in sections 2 and 4.
