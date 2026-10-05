# Style guide

This file is injected verbatim into every writing prompt (chapter notes, synthesis, audit).
Edit it to change how summaries read — no code changes needed. Re-render an existing
book with the new style using `./run.sh <book> --from synthesize` (or `--from extract`
to redo the chapter notes too).

## Reader
- A busy executive who wants the book's ideas, evidence and practical use, not a book report.
- Assume intelligence, not prior knowledge of the subject.

## Tone
- Plain English. Short, direct sentences. Active voice.
- No fluff: never write "In this chapter, the author…", "This book explores…", "It is important to note…".
- No hype adjectives ("groundbreaking", "powerful", "fascinating"). Let the ideas carry weight.
- Use the book's own terms for its concepts; define each one the first time it appears.

## Depth
- Dense: every sentence should carry an idea, a number, a mechanism, or an example.
- Explain *why* an idea works, not just *what* it is.
- Keep specifics — names, numbers, studies, steps — rather than generalising them away.
- Separate what the author claims from what the evidence shows.

## Length
- Each part of a summary is a 20–30 minute read. The model first decides how much time a book deserves
  (20 minutes up to 2 hours) and the summary is split into 30-minute parts: Part 1, Part 2, … (at most 4).
- Chapter summary: dense; its length follows the book's plan (shorter when a book has many chapters).
- TL;DR: exactly 5 bullets, each under 25 words.
- Core thesis: one tight paragraph (80–150 words).
- Cheat sheet: fits on one printed page — 10–12 one-line entries.

## Formatting
- Bullets over paragraphs whenever the content is a list.
- Bold (**like this**) sparingly for key terms only. No other markdown, no headings, no HTML.
- Takeaways start with a verb ("Ask…", "Measure…", "Stop…") and are specific enough to do this week.

## Critique
- Fair and specific: name the strongest idea, the weakest evidence, what is missing, and who should skip it.
