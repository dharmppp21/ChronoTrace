# ADR-0003: Deduplicate values by content, never by identity

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** dharmppp21
- **Supersedes / Superseded by:** none

## Context

ADR-0001 committed to omniscient recording, and named sound change detection as
*the* phase-1 risk: day 3 measured naive capture of every local on every line at
**2,370×** on a realistic workload, because a 1,200-element list was re-walked on
all 13,210 lines though it never changed. The architecture is dead without a way
to stop paying for values that did not change.

The day-8 brief recommended an **identity fast-path**: skip the re-capture for
immutable objects by trusting `id()` as a cache key. This ADR records why that
recommendation was not followed, because it is the kind of decision that looks
like a micro-optimisation and is actually a correctness boundary. The failure it
guards against -- showing the user a value that was there 200 events ago but is
not there now -- is the single worst thing a debugger can do: a *confident, silent
lie*, the same failure mode ADR-0001 rejected replay for.

## Decision

**A captured value is deduplicated on its *content* -- a 128-bit hash of its
serialised representation -- never on the identity of the object it came from.**
Every local is re-captured every line; identical content collapses to one
`ValueRef`; changed content gets a new one. There is no identity shortcut.

## Alternatives considered

### Identity fast-path for immutables (the brief's recommendation)

Cache `id(obj) -> ValueRef` for immutable objects and skip re-capturing on a hit.
**Rejected, because a sound version buys nothing and a useful version is a bug:**

- Immutable **atoms** (`int`, `str`, `float`) capture for free -- `capture()`
  returns them unwrapped, so there is no walk to skip. An identity cache over them
  saves nothing.
- Immutable **containers** (`tuple`, large `frozenset`) are the only values whose
  capture is expensive, and `id()` on them is **unsound** as a durable key: a
  non-immortal immutable can die and have its address reused by a *different* value
  between two line events -- a `x := ...` walrus inside a comprehension rebinds and
  frees within a single source line. The cache would then hand the old value's
  reference to the new value: a silent stale read.

So the shortcut is worthless exactly where it is sound and wrong exactly where it
would pay. Re-capture-then-hash costs more per line and is *always* correct; the
cost is bounded because `capture()` is bounded (`max_nodes = 512`).

### Key the cache on the raw serialised bytes (no hash)

A `dict[bytes, ValueRef]` is collision-proof. **Rejected:** the keys would be as
large as the values, so a byte-budgeted cache holds far fewer of them and its hit
rate drops. Hashing to a fixed 16 bytes lets the same memory hold ~10× more
entries -- which is the entire point of a cache.

### Python's `hash()`

**Rejected:** 64-bit (collision-prone at scale), salted per process (so two
recordings are not comparable), and defined by the user's `__hash__` for their
own types -- user code, which the capture layer bans from the hot path outright.

## Consequences

**What this buys us:** correctness by construction. A list mutated in place
(`a.append(2)`) keeps its `id()` but changes its content, so it always re-captures
to a new reference -- the stale-value bug cannot occur. Two distinct-but-equal
objects deduplicate for free, with no `__eq__` call.

**What this costs us:** `capture()` + hash runs on every local every line, even
for unchanged values, because proving a mutable did not change requires looking.
Day 8 measured the resulting recording-size cut at **97.9%** on the realistic
workload; day 9's scoping cut *how often* the cost is paid; day 40 must cut the
per-value price itself (measured at ~827 µs for a large value, `repr` dominating).

**The collision boundary, stated as a number:** with a 128-bit hash and a
deliberately extreme 10⁷ distinct values, P(collision) ≈ 1.5 × 10⁻²⁵ -- below the
uncorrectable-memory-error rate of the hardware the recording runs on. A collision
would show one value where another belongs (silent), so it is sized to be
unreachable, not merely rare. 64 bits (≈ 1 in 370,000 recordings) was not enough.

**Reversal trigger:** if day-40 profiling shows the content hash itself (not
capture) dominates, a per-binding identity fast-path restricted to the *immortal*
singletons (`None`, `True`, small ints) is sound and could be reconsidered -- but
it was measured to save nothing, so the bar is a real number, not a hunch.
