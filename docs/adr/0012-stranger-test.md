# ADR-0012: The stranger test — usability findings for the Phase 5 UI

**Status:** _draft — awaiting the session_ · **Date:** day 39 · **Context:** the roadmap's Phase-5
completion criterion is *"a stranger can open the UI, scrub to a bug, and understand what happened
without you narrating."* You cannot test that yourself — five days in, you can no longer see the UI.
So this ADR records an actual session with someone who has never seen ChronoTrace, and what changed
because of it.

> **How to run it.** Sit a real stranger (a friend, housemate, a Discord acquaintance) in front of
> `chronotrace record examples/buggy_pipeline.py --ui`. **Say nothing.** Do not help, explain, or
> defend. Write down every hesitation, wrong click, and "what does this do?" — verbatim, in their
> words. Then fill this file in and let it drive the fixes. Usability findings written down are
> engineering evidence; most portfolios have none.

## The subject (no PII — a role, not a name)

- Who: _e.g. "a backend engineer who has used pdb but never a time-travel debugger"_
- Prior exposure to ChronoTrace: _none / saw the README / …_
- Setup: _OS, browser, screen size; recording used_

## The session — observations, verbatim

Record what happened, in order, without interpreting yet. One row per moment that mattered.

| time | what they were trying to do | what they did | what they said | where they stalled |
|---|---|---|---|---|
| 0:00 | _first look at the empty/loaded UI_ | | | |
| | | | | |

_Aim for 8–15 rows. The stalls are the gold._

## The top confusions, ranked

The 3–5 findings worth acting on today, most-damaging first. For each: the symptom (what the
stranger did), the diagnosis (what the UI failed to say), and the fix (what shipped).

1. **_symptom_** — _diagnosis_ → _fix + commit_
2. …

Likely candidates the roadmap predicted (confirm or refute against the real session, do not assume):
- No empty state telling them what to do first.
- No visible hint that the timeline is draggable.
- `seq` leaking into the UI — a stranger does not care about an event index; show a step / position.
- Unlabelled panels.
- No indication backward stepping is even possible.

## What changed (the fixes)

Frontend fixes (built in Antigravity): _list them with commit refs_.

Backend/contract fixes, if any (e.g. an endpoint that returned data the UI could not render
cleanly, an error whose `detail` was not stranger-readable): _list them, or "none — the confusions
were all presentation"_.

## What was left unfixed, and why

Not every confusion is worth fixing before the demo. Record the ones deferred and the reason
(scope, cost, "the tour covers it"): _…_

## Decision

_The UI narrates itself if a video viewer understands it without a voiceover. State whether that bar
is now met, and what evidence says so (a second stranger? the recorded session re-watched?)._

**Reversal / re-test trigger:** re-run the stranger test after any change that alters the first-run
experience (empty state, tour, default panel layout). The first ten seconds are the only ten seconds
you never get to explain.
