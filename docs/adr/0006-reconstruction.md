# ADR-0006: Reconstruction — any `seq` to the program state at that instant

- **Status:** Accepted (design; implementation day 20)
- **Date:** 2026-07-19
- **Deciders:** dharmppp21
- **Supersedes / Superseded by:** builds on ADR-0004 (format), the keyframe (day 15) and
  delta (day 16) designs

## Context

Everything the user touches — the scrubber, backward stepping, the variable panel, the
call stack — is one function: **`reconstruct(seq)` → the program state after event
`seq`.** Phase 2 built the substrate (keyframes as floors, invertible deltas as the
changes between them). This ADR specifies the algorithm that turns that substrate into
the product, proves its cost, and decides the two things that will otherwise be guessed:
how to step backward, and how to cache.

## 1. The state model

**Program state after `seq`** (`reconstruct.types.ProgramState`) is:

- the **live frames** — for each: `frame_id`, `code_id`, current `lineno`, `parent_id`
  (call tree), `suspended` (a yielded generator is live but on no stack), and its
  **bindings** `name_id → value_ref`;
- the **current frame** — the one that executed the event at `seq`;
- any **in-flight exception**.

Two decisions fixed here:

- **The instant is "after `seq`", identical to a keyframe.** Reconstruction *starts* from
  a keyframe, so if the word differed by one, every scrub would be off by one event. The
  types assert `from_keyframe(kf).seq == kf.seq`.
- **Ids, not resolved values.** A `ProgramState` carries `name_id`/`code_id`/`value_ref`,
  never a variable name, source line or Python object. Resolution is a higher layer's
  job (the pool for `value_ref`, the string tables for `name_id`, the call-tree index for
  `parent_id`), so `server` serialises a state without importing a storage type and a
  state stays small enough to cache and diff.

## 2. The forward algorithm, and its cost

```
reconstruct(seq):
    kf    = reader.nearest_keyframe_at_or_before(seq)   # O(log K)
    state = ProgramState.from_keyframe(kf)              # O(F + B)
    for change in reader.deltas_between(kf.seq+1, seq): # ≤ I by the cadence invariant
        state = apply(state, change)                    # O(1) amortised
    state = overlay_control_flow(state, kf.seq+1, seq)  # ≤ I events
    return state
```

Let `E` = total events, `I` = keyframe interval (default 1000), `K = E/I` = keyframes,
`F` = live frames at the instant, `B` = total bindings (both bounded by program
structure, not by `E`).

| line | cost | why |
|---|---|---|
| nearest keyframe | **O(log K)** | binary search over the sorted keyframe seqs (day 15) |
| decode keyframe | **O(F + B)** | copy the live frames and their bindings |
| deltas replay | **O(I)** amortised | ≤ I deltas, each a copy-on-write of one frame |
| control-flow overlay | **O(I)** | ≤ I events, each an O(1) update |
| **total** | **O(log K + I + F + B)** | logarithmic in recording length + a bounded loop |

**Proof of the ≤ I bound (from the day-15 cadence invariant).** Keyframes are emitted at
every `seq` with `seq mod I == 0`, i.e. at `0, I, 2I, …`. For any target `seq`, the
nearest keyframe at or before it sits at `k = ⌊seq/I⌋·I`, so `seq − k = seq mod I ≤ I−1`.
`deltas_between(k+1, seq)` therefore spans at most `I−1` events, and deltas are a subset
of events, so **at most `I−1` deltas are applied.** ∎

*Graceful degradation:* if keyframe `k` failed CRC or was lost in a truncated tail,
`nearest_keyframe_at_or_before` falls back to the previous intact keyframe at
`k − m·I` (day 15). The replay is then `≤ (m+1)·I` — bounded by the number of
*consecutive* damaged keyframes, which is 0 in a healthy recording and small even in a
damaged one. Never unbounded.

**The whole product in one sentence:** *to reach `seq` 500,123 we binary-search to
keyframe 500,000 (one `O(log K)` lookup) and apply the ≤ 123 deltas since — at most
1,000 by the cadence, never 500,123.*

### The control-flow overlay (a design finding)

Keyframes and deltas carry the **data-flow** state exactly: bindings and which frames are
live. They do **not** carry, between keyframes, each frame's *current line*, the
*current-frame pointer*, the *call parent* of a frame that entered after the keyframe, or
an *in-flight exception* — those are control-flow, and the delta model (day 16) kept
deliberately minimal (a per-line delta would explode the stream). So reconstruction
**overlays** them from the events in the same `(kf.seq, seq]` window: `CALL` fills a new
frame's `code_id`/`parent_id`, `LINE` advances a frame's `lineno` and the current
pointer, `RAISE`/`UNWIND`/`EXCEPTION_HANDLED` drive the exception. The overlay is the
same ≤ I events, so the bound is unchanged. (`parent_id` for a frame that entered *before*
the keyframe is unavailable from the keyframe's flat frame set; it becomes authoritative
only with the day-27 call-tree index, and is best-effort `None` until then — tracked.)

## 3. Backward stepping — the day's real decision

- **(a) Re-reconstruct at `seq−1`.** Simple, always correct, `O(log K + I)` per step.
- **(b) Invert the delta at `seq`.** From the cached state at `seq`, invert the (usually
  one) delta whose `seq` is the current instant — `apply(state, inverse(d))`, day 16's
  `old_value_ref` is exactly what makes this possible — and set the current line/frame
  from `event[seq−1]`. **O(1) per step.**

**Decision: ship (b); keep (a) forever as the oracle.** A playhead drag is one step at a
time, so backward stepping must be O(1), not a fresh `O(I)` reconstruction — this is
precisely why day 16 spent bytes on `old_value_ref`. But (b) is the subtle path (does the
inverse truly reverse the forward step? does the incremental cache drift?), and (a) is
obviously correct. So the day-20 test asserts **(b) == (a) at every `seq`** across the
test recordings — *differential testing*: a fast implementation checked against a slow,
plainly-correct reference. Keeping the ~10-line oracle forever is not waste; it is how
production interpreters, compilers and databases guard an optimised path, and it runs
only in tests.

## 4. The locality cache

Scrubbing is overwhelmingly **local**: the user drags one event at a time. So keep the
**last reconstructed `ProgramState`** and serve the next request relative to it:

| next `seq'` vs cached `seq` | action | cost |
|---|---|---|
| `seq' == seq` | return the cached state | O(1) |
| `seq < seq' ≤ seq + I` | advance forward: apply deltas `seq→seq'` | O(seq'−seq) |
| `seq − I ≤ seq' < seq` | step backward: invert deltas `seq→seq'` | O(seq−seq') |
| otherwise (a jump) | fresh reconstruct from the nearest keyframe | O(log K + I) |

**Invalidation rule, stated precisely.** The recording is **append-only and immutable**,
so a cached state never goes *stale* with respect to the data — there is nothing to
invalidate on that axis. The rule is only *when to advance incrementally vs restart*:
restart from a keyframe whenever `seq'` falls outside `[seq − I, seq + I]`, because beyond
that window a fresh reconstruction (`≤ I` work) is cheaper than the incremental walk. The
window is `I` so incremental work never exceeds a fresh reconstruct's.

**Memory bound: one `ProgramState`** — `O(F + B)`, the live stack and its locals, which is
bounded by program structure, not by recording length. (A small LRU of a few states is a
future option; one covers drag-locality and keeps the bound trivial.)

**The risk being taken on, named.** An incrementally-advanced cached state that
accumulates a bug **drifts silently** — it is still a plausible state, just wrong, which
is the one unforgivable failure for a debugger. Correctness is *defined* by the
from-keyframe reconstruction; the incremental path is an optimisation that **must equal**
it. That equality is enforced by the day-20 oracle (§3) and the day-22 replay-equivalence
harness (§6). This is exactly why those two exist.

## 5. The interface

`reconstruct.types.Reconstructor` is a `Protocol` with `reconstruct(seq) -> ProgramState`
(and day 20 adds `step_back`/`step_forward` for the cache-friendly path). `ProgramState`
is the DTO `server` will serialise via `as_dict()` — an ids-only JSON shape, pinned by a
golden test, so the wire contract is explicit and no storage type leaks upward.

## 6. Failure modes and edge cases

| case | behaviour |
|---|---|
| `seq = 0` | keyframe 0 always exists (day-15 floor); apply 0 deltas |
| `seq` beyond the recording / in a lost tail | `IndexError` — no state exists there |
| nearest keyframe failed CRC | fall back to the previous intact keyframe, replay further (§2) |
| suspended generator | present in `frames`, `suspended=True`, not the current frame |
| frame entered before the keyframe, exits after `seq` | in the keyframe, stays live (no EXIT delta ≤ `seq`) |
| frame exits before `seq` | a FRAME_EXIT delta removes it from the replay |
| async interleaving | several frames live; `current_frame_id` = the one that ran `event[seq]` |
| exception in flight at `seq` | set from the `RAISE` overlay, cleared by `HANDLED`/unwound past |

## 7. The day-22 replay-equivalence plan (specified now)

The strongest proof of reconstruction is comparison against **reality**: during recording
the recorder observes the live `f_locals` of every frame at every event — that *is* the
ground-truth `ProgramState`. So:

1. Instrument a recording to snapshot the ground-truth state (live frame set + bindings)
   at a sample of seqs, stored alongside the `.chrono`.
2. Assert `reconstruct(seq)` equals the observed live state at every sampled `seq`.
3. Plus the two internal checks: the backward oracle `(b) == (a)` at every `seq`, and
   forward reconstruction against a from-`seq`-0 replay reference on a small recording.

If (1)–(3) hold, reconstruction is not merely self-consistent — it matches what actually
happened. Built day 22.

## Amendment (day 20, on implementing this)

Building the fast path and running it against the oracle immediately found **two fields
that were not a pure function of `seq`** — they depended on *how* the instant was
reached. That is a correctness bug of the worst kind for a debugger (the same instant
renders differently depending on how you scrubbed there), and the oracle earned its cost
in the first hour by catching both.

1. **`parent_id` — removed from `ProgramState`.** A keyframe stores a flat frame set with
   no call links, so a frame that entered *before* the keyframe had a parent when replayed
   from zero and `None` when started from the keyframe. Since §1 already named the day-27
   call-tree index as the *authority* for parents, the honest fix is to stop carrying a
   field reconstruction cannot fill path-independently. The call tree comes from the index.
2. **The in-flight exception — added to the keyframe (format 1.4).** Same flaw, but with
   no other authority: an exception still propagating across a keyframe was visible when
   replaying from zero and invisible when starting from that keyframe. Nothing else can
   supply it, and an in-flight exception is genuinely part of live state — so the keyframe
   now records `(exc_type_id, raised_at_seq)`. A keyframe claims to be "the complete live
   state at an instant"; it was incomplete.

3. **A latent delta-derivation bug (day 16), on real data.** `derive` emitted an implicit
   `FRAME_ENTER` only for `VAR_WRITE`, while `LiveState` creates a frame on *any* event —
   so a frame whose first event was a `LINE` (recording began mid-execution) had no ENTER
   delta, and a later BIND raised `DeltaError`. Only the from-zero path hit it; the
   keyframe path inherited the frame from the snapshot and hid it entirely.

The general rule this establishes, worth stating once: **anything in `ProgramState` must
be reconstructable from the keyframe plus the bounded window, or it does not belong in
`ProgramState`.** A field that only *some* paths can fill is not state.

The measured outcome (benchmarks/RESULTS.md) also refined the cache: a playhead drag was
3.7 ms until `deltas_between` was given the same block-level LRU that EVENTS blocks had —
a drag re-decoded one DELTAS block thousands of times. It is now **119 µs**.

## Amendment (day 21, on implementing backward stepping)

**§3's decision is reversed: backward stepping does *not* invert deltas. It reconstructs
at its destination like every other jump.** The reversal is on measurement, and the
premise that failed is worth naming: §3 assumed the delta replay dominates a backward
step. Splitting the cost (benchmarks/RESULTS.md) shows it does not, for a structural
reason §3 did not see —

> **The control-flow overlay is not invertible.** Deltas store `old_ref` precisely so
> bindings can be undone. An event stores no *previous* `lineno`, so each frame's current
> line must be re-derived from the keyframe however the bindings got there.

| phase of a backward step | cost | invertible? |
|---|---:|---|
| delta replay | 231 µs | yes — all inversion could remove |
| event overlay | 487 µs | **no** |

Inversion's ceiling is therefore **32%** of an operation measured at **715 µs p50 / 1,521
µs p99 — 11× inside a 60 fps frame budget**. Trading a second incremental state machine
(the silently-drifting cache §4 calls unsurvivable) for ~230 µs nobody can perceive is a
bad trade, so it was not built. Day 16's `old_ref` is not wasted: `invert` is what day
22's replay-equivalence harness checks against, and it is the lever to pull if the overlay
is ever promoted into the keyframe.

**§2's reversal trigger has fired.** It read: *"if the control-flow overlay's event scan
ever dominates the profile, promote the needed control-flow (current line per frame) into
the keyframe or a new delta kind."* It now does dominate — 68% of a backward step. Not
acted on today because the operation is already 11× inside budget; recorded so the next
person to profile reconstruction finds the lever already identified rather than
rediscovering it.

### Stepping is a `seq` search, not a state walk

The four operations (`step`, `step_over`, `step_out`, `seek`) are one directional scan
over events, parameterised by the sign of the step, plus **one** `reconstruct` at the
destination. Two consequences:

- **Forward and backward cannot drift**, because they are the same function with the
  opposite sign — the property `step_back(step_forward(seq)) == seq` is enforced at every
  stop instant in every example recording.
- **Backward stepping adds no new way to be silently wrong.** Where to stop is a pure
  event query that cannot corrupt state; what the state is there is the already-proven
  `reconstruct`. A state walk would have had to do both at once, in lockstep.

`step_over` filtering on **`frame_id` and never `code_id`** is what makes recursion
correct (four frames share one code object in `examples/simple.py`) and what makes
`asyncio` interleaving free (other tasks' events are simply not in this frame).

### The known ceiling, deliberately not half-fixed

The scan is linear, so a `step_over_back` in a module-level frame scans 281k events and
costs **0.63 s** (p50 is 1.8 µs — the distribution is bimodal, not uniformly slow).
Iterating decoded blocks instead of `reader[seq]` would make it ~5× faster and still leave
126 ms, four times over budget. **The constant factor is the wrong lever**; only day 30's
line index, answering "previous LINE in frame F" as a lookup, fixes it. Tracked as issue
#5.

## Consequences

**Buys:** the product, at `O(log K + I)` — a binary search plus a bounded loop,
sub-linear in recording length — with O(1) backward steps and an interactive-feeling
scrubber.

**Costs:** two implementations of backward stepping kept forever (fast + oracle), and a
cache whose correctness rests on an equality that must be tested, not assumed.

**Reversal trigger:** if the control-flow overlay's event scan ever dominates the profile,
promote the needed control-flow (current line per frame) into the keyframe or a new delta
kind — the block-size ADR-0005 finding (block decode dominates) is the related lever.
