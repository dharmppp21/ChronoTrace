# ADR-0007: The halfway review — what drifted, and what I would build differently

- **Status:** Accepted
- **Date:** 2026-07-22 (day 24 of 50)
- **Deciders:** dharmppp21
- **Scope:** reconciles ADR-0001…0006 with the code as it exists at the end of Phase 3

## Context

Twenty-four days in, the engine works: a program can be recorded, stored durably, and
scrubbed backward instant by instant. Everything after this — queries, a UI, packaging —
is built on these interfaces, so **this is the last cheap moment to correct an
architectural mistake.** After Phase 4 the query API depends on them; after Phase 5 a UI
does.

The question this ADR answers is not "is the code good?" but the harder one: *which
decisions stopped being true, and did the implementation quietly drift away from what is
written down?* A decision that silently stopped being true is worse than no decision at
all, because someone will trust it.

## 1. ADR reconciliation

| ADR | Verdict | Action |
|---|---|---|
| 0001 recording strategy | **holds** — omniscient recording, not deterministic replay. The <20× flow-only trigger is not tripped: measured **6.7×** today | none |
| 0002 frame registry | **drifted twice** | two amendments, below |
| 0003 dedup correctness | **holds** — content-addressed, no identity shortcut. Day 22 injected the identity shortcut deliberately and the referee caught it | none |
| 0004 `.chrono` format | **holds** — framing, CRCs and the index-written-last rule are unchanged since day 11 | spec bumped to 1.5 |
| 0005 storage defaults | **holds** — `block_events=4096`, `keyframe_interval=1000`, `level 9` all match the code | none |
| 0006 reconstruction | **already amended** days 20 and 21; §3's backward-stepping decision was *reversed* on measurement | none |

### ADR-0002, drift 1 — an imprecise heading that was actually wrong

The rejected-alternatives section was headed *"`id(frame)` as the key"*. The registry
**does** key its live map on `id(frame)`; what is rejected is `id(frame)` as the *durable
identity* handed to events. Those are different claims and the ADR blurred them, which
matters because the safety argument lives entirely in the distinction. Corrected in place
with a dated amendment.

### ADR-0002, drift 2 — the safety argument had a hole, found on day 22

The argument was: *a reused address always finds an empty slot, because the map is swept
on frame death.* That holds only if every frame that enters also exits — and the same ADR
records a case where one does not (CPython < 3.13 emits no `PY_UNWIND` for a
GC-finalised generator). CPython then reused the freed address, `enter()` found the stale
entry, and a brand-new frame inherited a dead frame's `frame_id`: **two unrelated frames
fused**, the exact failure the ADR exists to prevent.

Fixed on day 22 (`enter(..., resuming=)`); recorded here because the *decision* was right
and the *reasoning* had an unstated premise. Those are worth separating.

## 2. Format spec, verified line by line

Every structural claim was checked against `constants.py` rather than read: 32-byte
header, 12-byte block header, 32-byte EOCD, 14-byte index entry, block type values
`0x0001`–`0x8002`, little-endian throughout. **All match.**

One update was needed, and it is the interesting kind. Day 24 gave `VAR_WRITE` a second
meaning — no `value_ref` now means the binding was *removed* — which changed **no bytes
at all**: the column already held `-1` for events with no value. A change that alters
nothing on disk but alters what the bytes *mean* is the most dangerous kind a format can
undergo, because nothing looks different. It took a version number (**1.5**) precisely
for that reason.

## 3. The honest question: what would I build differently?

### Things I would change, and did today

**Two implementations of "what does this event do to bindings".** `LiveState.apply`
(which builds keyframes) and `delta.derive` (which builds the delta stream) each decided
independently whether an event binds. They are the two halves of one codec; had they ever
disagreed, a keyframe would claim one state and replaying the deltas would produce
another — a divergence the referee would catch and nobody could locate, because both
sides look locally correct. The rule now lives once, in `keyframe.binding_change`.

This was not hypothetical: fixing `del` required changing that rule, and with two copies
the fix would have landed in one of them.

### Things I would change, and deliberately did not

**The recorder should have had a "state projection" concept from day 6.** `LiveState`
(day 15), `delta.State` (day 16) and `reconstruct.Work` (day 20) are three views of the
same idea, arrived at three times because each phase discovered it separately. They are
*not* redundant — each carries something the others do not — but a single vocabulary
would have made the relationship obvious rather than something you infer. Not worth
unifying now: the seams are correct, only the naming is archaeological.

**Value capture is still the dominant cost** (measured 1440× with capture, 6.7× without).
Everything else has been tuned; this has not, because days 40–41 own it and optimising
before the profiler says where is how you make code fast in the wrong place.

### The thing I got most right, and would do again first

**Building the referee before the features that need it.** The equivalence harness
(day 22) found a real defect on its first run, and the property campaign (day 23) found
seven more. Every subsequent change — including today's `del` fix and the frame-fusion
fix — was validated by machinery that already existed. The alternative, writing it after
Phase 4, would have meant discovering these bugs with a query engine layered on top of
them.

## 4. Scope: what ChronoTrace is not

The risk register flagged **"scope creep into a full IDE"**, and Phase 5 is the moment
that risk becomes real, so the boundary is committed to here, in writing, before the UI
exists to tempt anyone:

> **ChronoTrace is a scrubber over a recording. It is not an IDE, not an editor, and not
> a live debugger.**

Concretely, out of scope for the whole project:

- **Editing code**, or running it. There is no "restart with this change".
- **Live/attached debugging** — no breakpoints that pause a running process, no `continue`
  that resumes one. Every breakpoint is *retroactive*, evaluated against a finished
  recording (day 30). That is the thesis, not a limitation.
- **Modifying the past.** No "set variable and continue". The recording is append-only and
  immutable, which is what makes reconstruction a pure function of `seq`.
- **A project explorer, terminal, extension API, or plugin system.**

The UI is a timeline, a source pane, a variable panel and a call tree — four surfaces over
`reconstruct(seq)`. If a Phase 5 day proposes a fifth, it needs an ADR, not a commit.

## Decision

1. Amend ADR-0002 twice (in place, dated). No superseding ADR: the decisions stand, the
   reasoning needed correcting.
2. Bump the format spec to **1.5** and record why a semantics-only change takes a version.
3. Collapse the duplicated binding rule into `keyframe.binding_change`. **Done today.**
4. Fix the `del` blind spot ([#7](https://github.com/dharmppp21/ChronoTrace/issues/7)) —
   the one defect that made the debugger actively lie. **Done today.**
5. Commit to the scrubber boundary above for Phases 4 and 5.

## Consequences

**Buys:** every ADR now matches the code; one authority for the binding rule; a debugger
that no longer lies about deleted variables; a written scope boundary to point at when
Phase 5 gets tempting.

**Costs:** a format version for a change that altered no bytes — the honest price of
having a spec at all.

**Open, with issues and repros rather than silence:**
[#5](https://github.com/dharmppp21/ChronoTrace/issues/5) stepping's linear scan (day 30
indexes it), [#6](https://github.com/dharmppp21/ChronoTrace/issues/6) intern tables not
persisted (blocks the day-33 server),
[#8](https://github.com/dharmppp21/ChronoTrace/issues/8) a caller's locals go stale while
a callee mutates them, [#9](https://github.com/dharmppp21/ChronoTrace/issues/9) aliasing
back-references are anonymous (Phase 5 needs it to draw the badge).
