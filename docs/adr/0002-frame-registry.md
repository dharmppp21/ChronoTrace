# ADR-0002: A live-frame registry, not a call stack

- **Status:** Accepted
- **Date:** 2026-07-13
- **Deciders:** dharmppp21
- **Supersedes / Superseded by:** none

## Context

The recorder must give every function invocation a stable identity -- a
`frame_id` -- so the call tree (day 27) can store `entry_seq`/`exit_seq` per frame
and answer "which call was this?". The obvious model is a call stack: push on
`PY_START`, pop on `PY_RETURN`. It is what a synchronous debugger uses, and it is
wrong for Python, which day 6 demonstrated rather than assumed.

`sys.monitoring` (PEP 669) delivers `PY_START`, `PY_RETURN`, `PY_YIELD`,
`PY_RESUME` and `PY_UNWIND`. Three real cases break a stack:

1. **A generator yields.** Its frame leaves execution at `PY_YIELD` but does not
   die; it resumes later at `PY_RESUME`. A stack would pop it at the yield and
   have nothing to recover on resume.
2. **Two generators of the same function interleave.** `a = numbers(3)`,
   `b = numbers(3)`, then `next(a), next(b), next(a)`. Two distinct live frames
   share one code object and take turns -- LIFO cannot represent it, and keying on
   the code object would fuse them into one node describing a program that never
   ran.
3. **A generator is abandoned while suspended.** Garbage collection throws
   `GeneratorExit`, so the frame unwinds (`PY_UNWIND`) without ever re-entering --
   an exit with no matching top-of-stack entry, which sends a depth counter
   negative on correct behaviour.

## Decision

**The recorder tracks a registry of live frames, and assigns each frame a stable
monotonic `frame_id` that persists across suspend/resume for the frame's whole
life.** Ordering across interleaved frames is carried by the global `seq` clock,
not by frame structure. The registry is cleaned when a frame dies (`PY_RETURN` or
`PY_UNWIND`), so it stays bounded by the number of *live* frames.

## Alternatives considered

### A call stack (push/pop)

What a synchronous stepping debugger uses. **Rejected:** it cannot model a
suspended generator, interleaving of two frames of one code object, or an
abandoned generator unwinding out of order. All three are ordinary Python, not
edge cases, and each corrupts the call tree silently -- the frame structure would
describe an execution that did not happen.

### `id(frame)` as the *durable identity*

Frames are cheap to identify by address. **Rejected on two measured facts:**
CPython reuses frame addresses, so `id()` collides across a recording; and frame
objects are **not weakref-able**, so we cannot detect reuse the way `identity.py`
does for user objects. A reused `id()` would fuse two unrelated calls.

> **Amendment (day 24, halfway review).** This heading said "as the key", which was
> imprecise enough to be wrong: `id(frame)` *is* the registry's live-map key
> (`FrameRegistry._live`), and only the durable `frame_id` handed to events is a
> counter. The distinction is the whole safety argument — an address may be reused
> once its previous owner is gone, and the map is swept on frame death, so a reused
> address always finds an empty slot. See the day-22 amendment below for the case
> where that assumption failed.

### The code object as the key

One key per function, cheap and stable. **Rejected:** recursion and multiple live
generators put many live frames behind one code object, and this fuses them --
exactly the day-6 counter-example.

## Consequences

**What this buys us:** generators, coroutines and recursion all get correct,
stable identities; `seq` is established as the only thing that can order
interleaved execution, which every later phase relies on; the call tree can be
built from `entry_seq`/`exit_seq` without lying.

**What this costs us:** a per-thread execution structure plus a process-wide
identity map, more moving parts than a stack; and the registry must be swept on
death or it leaks. The frame invariants (`tests/recorder/invariants.py`) are
checked per-frame ("a frame's life story is `CALL (YIELD RESUME)* (RETURN|UNWIND)`")
rather than as a global depth, precisely because a depth counter reintroduces the
stack's bug in the test.

**What this forces later:** day 27's call-tree index keys on `frame_id` and
assumes every frame has both an entry and an exit `seq`; that assumption is only
safe because the registry guarantees a death event.

**Reversal trigger:** none foreseen -- the stack model is disproven by cases the
language guarantees, so this is effectively permanent. One dated limitation is
recorded instead: on **CPython < 3.13**, `sys.monitoring` does not emit
`PY_UNWIND` when a generator is finalised by GC, so an abandoned generator's frame
leaks on 3.12 only (fixed in 3.13; pinned by an xfail test). It is unfixable at
the recorder level there, because no event fires and frames cannot be weakref'd.

## Amendment (day 22) — the leaked entry made address reuse bite after all

The safety argument above ("a reused address always finds an empty slot, because the
map is swept on frame death") holds only if **every frame that enters also exits**. The
limitation in the previous paragraph is exactly a case where one does not: on 3.12 an
abandoned generator's entry leaks. CPython then reused the freed frame's address, the
registry found the stale entry, and `enter()` handed a brand-new frame the dead one's
`frame_id` — **fusing two unrelated frames**, which is precisely what this ADR exists to
prevent, arriving through the back door.

Found by the day-22 equivalence harness as `FRAME_ENTER for frame 9, which is already
live`, and latent since day 6. Fixed where the callers already knew the answer:
`enter(frame, *, resuming=)`. A `PY_START` is by definition a frame that did not exist a
moment ago, so it never recovers an id; only `PY_RESUME` does, which is the recovery this
ADR is *for*. The decision stands; the implementation was reasoning from an assumption
the interpreter does not always honour.
