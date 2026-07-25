# ADR-0009: Dogfooding findings (day 31, checkpoint 4)

**Status:** accepted · **Date:** day 31 · **Context:** the Phase 4 completion criterion —
*"debug a real bug in `examples/` using only queries: no prints, no re-running."*

## What was done

Three programs with bugs the debugger had not seen were written (`examples/mystery/`), each
producing a plausible-but-wrong output and no crash: an off-by-one loop bound, a mutable
default argument, and a late-binding closure. Each was diagnosed with **one or two queries**,
reading no source and re-running nothing. The winning queries are frozen as
`tests/query/test_mystery_regressions.py`, so the demo cannot silently rot.

## What worked

- **`--watch NAME` and `--var-writes NAME` are the workhorses.** For a state bug, the entire
  history of a variable in one query — with old→new values — put the defect on screen every
  time. The off-by-one was two queries; the other two were one each.
- **The `[seq]` on every row meant the next question was always cheap** — each answer pointed
  at the instant to ask about next.
- **No re-running was the felt difference.** Every `pdb` session of these bugs involves
  running to the right call and stepping; here the whole timeline was already there.

## What did not work — the gap list (the real output)

1. **Captured values rendered as raw tagged dicts.** A list showed as
   `{'$': 'list', 'items': ['first'], 'len': 1}` instead of `['first']`. This was the single
   worst readability problem — the tag is right on disk (pure data, day 7) and wrong on
   screen. **Fixed today:** `query/_resolve.render_captured` unwraps captured containers back
   to Python-shaped text, and every query preview goes through it. Cheap, real, done.
2. **Guessing a line number without the source.** `--line-hits file:LINE` needs the line, and
   with no source pane you guess and miss (I hit the wrong line once). This is the day-35
   source pane ([#12]); noted, not fixed today — it needs UI that does not exist yet.
3. **Aliasing is inferred, not shown.** The mutable-default bug was found from *values*
   (`['first']` at the second call), but "these two calls hold the *same* list object" is not
   stated directly — the identity badge is [#9], scheduled for day 35. The value was enough
   here, but a pure aliasing bug with equal-looking distinct objects would be harder.

## Where ChronoTrace loses to `pdb` (stated on purpose)

A tool author who knows exactly where their tool loses is more credible than one who claims
it always wins. `pdb` is the better tool when: you can attach to an **already-running**
process; you want **zero setup** and **zero overhead**; or the bug lives somewhere
ChronoTrace does not record — **thread interleavings** beyond the observed event order, **C
extension internals**, **timing-dependent races**, or **memory/refcount** behaviour. These
are recorded honestly in the README comparison table, not hidden.

ChronoTrace wins when the bug is a **state** or **causal** question, especially when cause
and symptom are far apart in time — the case where "re-run and set a breakpoint earlier" is
the loop it removes.

## Decision

Phase 4 meets its completion criterion: three unseen bugs, queries only. The one cheap gap
(value rendering) is fixed; the two expensive ones ([#9], [#12]) were already scheduled for
day 35 and stay there. No query-engine rework was done — the engine had no hole, only a
display wart.

[#9]: https://github.com/dharmppp21/ChronoTrace/issues/9
[#12]: https://github.com/dharmppp21/ChronoTrace/issues/12
