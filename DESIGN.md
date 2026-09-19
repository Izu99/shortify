# Shortify — design direction

## Reference (Step 1)

**Aegisub's audio timing / karaoke-splitting panel** — https://aegisub.org

A specific real artifact, not an adjective string. Chosen because it is literally the tool
the ASS subtitle format came from, and because its central idea is the one this app needs:
*a line of dialogue is not text, it is a row of syllables each occupying a measurable slice
of time.* Aegisub shows those slices as proportional blocks you can see, click, and nudge.

Sibling to **Ferry** (whose reference is TeraCopy) — same cool-graphite instrument family,
deliberately different accent and different signature.

## What is actually taken from the reference

Not the waveform. The **proportional block strip**. In Aegisub a syllable's block is as wide
as the time it occupies, so timing errors are *visible as shape* — a word that hangs too long
is a fat block, a gap where no caption shows is a hole in the strip. You do not read numbers
to find the problem, you see it.

That is the whole reason this app exists rather than the CLI. The CLI already burns captions
correctly. What it cannot do is let you *see* that word 41 lands early.

## Signature move

**The word ribbon.** A horizontal, time-proportional strip across the full width of the
window: one chip per word, chip width = that word's duration, gaps drawn as empty ground.
Click a chip to seek the preview and select the word for editing. The ribbon is the
navigation, the timing display, and the error surface, all one object.

Deliberate exception: the ribbon does not appear before a transcript exists — the empty
state is a drop target, not an empty ribbon.

## Tokens

Ground is the same cool graphite as Ferry — neutrals carry blue temperature, an instrument
reading rather than a document. Consistency across the user's own apps beats novelty.

- **Single accent: `@signal` = #FFD400**, the caption yellow. Not decorative — it is the
  literal color the app paints onto video. The accent and the output are the same thing.
- Functional states only: `@drift` (amber, word timing suspicious), `@edited` (green, word
  corrected by hand), `@gap` (faint, no caption showing).
- Shadows derive from the ground hue, never black.
- Radius is a semantic scale: 0 on ribbon chips and ledger rows, 2px data chrome, 4px controls.

## Type

Two families, separate jobs — same split as Ferry:

- **UI/body** — system UI stack (Ubuntu/Cantarell on Mint).
- **Data** — monospace, used *only* where values must align in a column: timecodes, durations.
  Never for labels. Timecodes are load-bearing here (the product is timing), so monospace is
  structural, not garnish.

Scale: 20 / 15 / 13 / 11 — each step ≥1.25× its neighbour. Title 800 weight against body 400.
