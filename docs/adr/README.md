# Architecture Decision Records

An ADR captures **one decision that would be expensive to reverse**, at the moment
it was made, with the alternatives that were rejected and why.

## When to write one

Write an ADR when a choice is expensive to undo later:

- an on-disk or wire format (someone else's files depend on it forever)
- a correctness rule the whole system relies on
- a dependency, a language, or a build tool
- an algorithm whose complexity is a user-facing promise
- **a decision to *not* build something** — these are the most valuable and the
  most commonly lost

Do **not** write one for a choice you could reverse in an afternoon. A log full
of trivia is a log nobody reads.

## Rules

1. **Write it the day you decide**, not later. Reasoning does not survive ten
   weeks; you will remember the decision and forget why you rejected the
   alternative. That "why" is the entire value.
2. **Name the rejected alternatives.** An ADR without them is an announcement.
3. **ADRs are immutable once merged.** Reality changed? Add a dated amendment, or
   write a new ADR that supersedes it and link both ways. Never edit history —
   the point is to show what was known at the time.
4. **State the reversal trigger** where one exists: "revisit this if X." A
   decision with a trigger is engineering; a decision without one is dogma.

## Format

Copy [`0000-template.md`](0000-template.md). Four sections: Context, Decision,
Alternatives considered, Consequences. Number sequentially. Keep it under a page —
if it needs more, the decision is probably two decisions.

## Log

Numbered ADRs begin at 0001 (Phase 0, day 3). This log is the design story of
the project: read in order, it explains how each decision forced the next.

| # | Decision | Status |
|---|---|---|
| [0001](0001-recording-strategy.md) | Omniscient recording, not deterministic replay | Accepted |
| [0002](0002-frame-registry.md) | A live-frame registry, not a call stack | Accepted |
| [0003](0003-dedup-correctness.md) | Deduplicate values by content, never by identity | Accepted |
| [0004](0004-chrono-file-format.md) | A purpose-built columnar `.chrono` format | Accepted |

## Baseline decisions not recorded here

Day 1's packaging decisions — `src/` layout, hatchling, ruff+mypy config, and the
zero-runtime-dependency rule — are documented **where they are enforced** rather
than in this log: in `pyproject.toml`'s comments and in the package docstrings in
`src/chronotrace/`. They live next to the code that would break if someone
ignored them, which is the only place a reader will actually look. The
zero-dependency rule in particular is a standing constraint, not a one-time
choice, so it belongs in the file that would have to change to violate it.
