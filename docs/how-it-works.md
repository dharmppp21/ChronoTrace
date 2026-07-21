# How ChronoTrace works

A time-travel debugger records a program once and then lets you move through its
execution in both directions. This explains how, from the top down — what is recorded,
how it is stored, how any past instant is rebuilt, and how we know the answer is true.

No prior knowledge of the codebase is assumed. Roughly a fifteen-minute read.

---

## The problem

Debugging only goes forward. A breakpoint shows you the program *now*, so the moment you
realise the bug happened two hundred steps ago, you restart and guess where to break —
over and over, each run a fresh attempt to be standing in the right place at the right
time.

Here is the shape of the problem, from `examples/buggy_pipeline.py`:

```python
totals = dict.fromkeys(REGIONS, {"sales": 0.0, "orders": 0})
for order in orders:
    bucket = totals[order["region"]]
    bucket["sales"] += order["amount"]
```

`dict.fromkeys` evaluates its default **once**, so all three regions share one dict.
Every region ends up holding the grand total. The mistake happens on line 1; the symptom
appears ~800 events later in a report that prints plausible, wrong numbers. It never
crashes.

In `pdb` this is genuinely nasty: step forward and every `+=` looks correct. To find it
you must already suspect aliasing and think to compare `id()` of the three buckets — you
must have guessed the answer before the tool helps.

The information you needed was computed, and thrown away. **ChronoTrace keeps it.**

---

## The four ideas

Everything below is one of four ideas stacked on each other:

1. **Record every event**, cheaply enough to be usable.
2. **Store it** so any instant is reachable without reading the whole file.
3. **Reconstruct** the program's state at any instant in bounded time.
4. **Prove it is right** against something other than itself.

---

## 1. Recording

Python 3.12 added [PEP 669](https://peps.python.org/pep-0669/), `sys.monitoring`: a
low-overhead hook that fires callbacks for lines, calls, returns, exceptions, yields and
resumes. ChronoTrace registers under its own tool id and turns those callbacks into a
stream of immutable `Event` records.

Every event carries a **`seq`** — a counter starting at 0, incremented once per event.
This is the single most important design decision in the project:

> **`seq` is the address of an instant in time**, and the primary key of everything above.
> The index is keyed on it, queries return it, reconstruction takes it as its only
> argument, backward stepping searches it, and it ends up in the browser's URL.

It is deliberately **not** derived from the clock. Wall clocks are not monotonic (NTP
steps, suspend/resume, VM migration), and — more fatally — two events can share a
timestamp, which means no total order, which means "the previous instant" stops being
well-defined. That is the one question this project exists to answer.

### Frames are a registry, not a stack

The obvious model for "which function are we in" is a stack. It is wrong for Python:

```python
def numbers(n):
    for i in range(n):
        yield i           # leaves the frame without exiting it
```

A generator is entered, *leaves without exiting*, and re-enters later — possibly
interleaved with other generators under `asyncio`. A stack says a frame is entered once
and exited once, LIFO. So ChronoTrace keeps a **registry of live frames**, and a frame
gets one stable `frame_id` for its whole life across every suspend and resume. Recursion
puts many live frames behind one code object; a suspended generator is live while sitting
on no stack at all.

### Values are captured, never referenced

When a local changes, we record what it *was*, as bounded plain data — never the object.
That capture never invokes the program's own code: no `__repr__`, no properties, no
`__getattr__`. A debugger that triggers side effects in the program it is watching is
worse than no debugger.

It is bounded by policy (depth 6, 100 items per container, 512 characters, and a **total**
budget of 512 nodes). That last one exists because depth and width limits bound each
*dimension* but not their product: `max_depth=6` with `max_items=100` permits 10¹² nodes,
and an ordinary 20×20×20×20×20 nested list measured at **26 seconds and 416 MB for one
variable on one line**.

### Two things that make it affordable

**Change detection.** A `VAR_WRITE` is emitted only when a binding's value actually
changed. Re-stating every local on every line measured at 2,370× overhead.

**Content-addressed deduplication.** Identical values are stored once and referenced by
integer. A loop that appends to a list produces one stored value per distinct state, not
one per iteration — measured at **−97.9%** on a realistic workload.

Deduplication is on **content**, never on object identity, and that distinction is a bug
we deliberately went looking for: a mutable object mutated in place keeps its `id()`, so
an identity shortcut would hand back the value from *before* the mutation. The
`buggy_pipeline` demo is exactly that shape.

---

## 2. Storage: the `.chrono` file

Events go into a binary file designed to be read partially and to survive a crash.
([Full normative spec](format-spec.md).)

```
┌─────────────────────────┐
│ Header (32 bytes)       │  magic, version, header size
├─────────────────────────┤
│ Block: EVENTS           │  ← length + CRC framed
│ Block: EVENTS           │
│ Block: KEYFRAMES        │
│ Block: DELTAS           │
│ Block: VALUES           │
├─────────────────────────┤
│ INDEX                   │  written last
│ EOCD (32 bytes)         │  present ⇔ closed cleanly
└─────────────────────────┘
```

### Columns, not rows

Within a block, events are stored **column-wise**: all the `seq`s together, then all the
`kind`s, then all the `lineno`s. Column-wise data compresses far better than row-wise
because a column is homogeneous — a run of identical `kind`s, a near-constant
`thread_id`, an arithmetic sequence of `seq`s. Measured **7–12× smaller** than row layout,
before general-purpose compression, which then gets a further ~4× on top.

Three codecs are chosen per column: raw, run-length, and delta-then-run-length. All three
earn their place on measured usage (60% / 30% / 10%).

### Surviving a crash

The index is written **last**, so its presence is the signal that the file closed
cleanly. If the process is killed mid-recording, the reader walks the blocks from the
header, verifying each CRC, and stops at the first torn one — recovering everything
written before the crash and reporting the recording as truncated. Verified by killing
50 real processes at random points.

The trade is deliberate: blocks are flushed to the OS but **not** `fsync`ed. A `kill -9`
leaves the bytes recoverable in the page cache; a power cut may not. Paying an fsync per
block would make recording unusably slow to defend against a case that loses your
unsaved editor buffer anyway.

---

## 3. Reconstruction: any instant, in bounded time

Here is the core question: **what was the program's state after event 487,332?**

The naive answer — replay every event from 0 — is O(seq), which for a million-event
recording means scrubbing a timeline gets slower the further right you drag. Unusable.

The answer is the one video codecs use.

### Keyframes and deltas

- A **keyframe** is a complete snapshot of live state, written every 1,000 events: every
  live frame, its current line, and all its bindings.
- A **delta** is one change — a binding that was written, a frame that entered, a frame
  that exited.

```
seq:  0        1000       2000       3000
      │K│░░░░░░│K│░░░░░░░░│K│░░░░░░░░│K│
       ↑        ↑
       keyframe  deltas between keyframes
```

To reach seq 487,332: binary-search to the keyframe at 487,000, decode it, and apply the
≤ 332 deltas since. Never 487,332.

**Cost: O(log K + I)** — a binary search plus a bounded loop, where `I` is the keyframe
interval. Sub-linear in recording length. Measured: **12 ms** for a cold random jump,
**65 µs** for a one-event step through the locality cache.

The interval is the one real tradeoff: keyframes more often means faster seeks and a
bigger file. Measured across a grid, 1,000 is the knee — 2.7 ms reconstruction for +4%
file size.

### Deltas are invertible, and that costs a byte

A delta stores the **old** reference as well as the new. That is a deliberate cost
(+0.15 bytes/delta, +9%) bought for one reason: a delta that stores only the new value can
be applied but not undone, so every backward step would have to rewind to the previous
keyframe and replay forward.

### Stepping backward

Every debugger command has a mirror image:

| forward | backward |
|---|---|
| `step` — next line, any frame | previous line, any frame |
| `next` — next line in this frame | previous line in this frame, skipping calls that completed |
| `finish` — run to where this frame exits | back to where this frame was called |
| `continue` — next breakpoint hit | previous breakpoint hit |

The implementation insight is that **stepping is a search over events, not a walk over
state.** "The previous line in this frame" is a question about the event stream; you
answer it first, then reconstruct **once**, at the destination.

This matters because of a measured number: the previous line *in the current frame* sits a
median of 1 event back, but up to **280,993** events back when you step over a call made
from a module-level frame. A state walk would invert a quarter-million deltas and
materialise a quarter-million intermediate states — every one discarded — to answer a
question that never needed a state at all.

Forward and backward are literally the same function with the sign of the scan flipped,
which is why they cannot drift apart. The property `step_back(step_forward(seq)) == seq`
is asserted at every stop instant of every example recording.

---

## 4. Proving it is right

This is the part I would most want a reader to take seriously, because a debugger that
lies is worse than no debugger: you will believe it, and then spend a day chasing a
variable that never had that value.

There are three layers of proof, and each catches what the one below cannot.

### Unit tests

Each piece against hand-written expectations. They cannot catch pieces that are
individually right and wrong *together*.

### The oracle: fast path vs. slow path

Reconstruction has two implementations. The fast one uses keyframes and deltas. The slow
one, `reconstruct_slow`, replays everything from seq 0 — O(seq), and obviously correct:
no keyframe to pick wrong, no window to get off by one, no cache to drift. The test
asserts they are **equal** at every boundary.

The slow one **ships**, permanently, with a docstring that says *do not optimise this*.
An unexplained slow function gets helpfully made fast by a future contributor, and the
moment it shares the fast path's cleverness it stops being able to catch the fast path's
bugs.

Writing it *first* paid for itself within an hour: it found three real defects, two of
which the fast path alone would have shipped silently.

But the oracle only proves the two paths agree with **each other**. If the *recorder*
misunderstood the program, both are confidently wrong together and every test stays green.

### The referee: reconstruction vs. reality

So a second observer runs during recording — a separate `sys.monitoring` tool, under its
own id, reading `frame.f_locals` directly. It imports none of the recorder's machinery:
not the frame registry, not the seq counter, not the value pool, not the dedup cache.

The reason is worth stating plainly. A truth source built from the recorder's own parts
would inherit the recorder's mistakes. If the frame registry fuses two frames, a truth
source that *asks the frame registry* which frame is current agrees with the fusion. The
test would assert `X == X`, pass forever, and ship a debugger that lies.

The property is one sentence:

> **At every sampled instant, the reconstructed state equals the state the program
> actually had.**

That spans every subsystem at once — recorder, capture, dedup, value pool, writer,
keyframes, deltas, reader, reconstructor.

**And it is proven to fail.** A test suite that has never failed is not evidence of
anything, so four bugs are deliberately injected — a dropped delta, a keyframe that
under-reports state, a dedup cache that cannot see a mutation, a reconstruction cache
that drifts by one — and the referee must go red for each. A fifth injection asserts the
opposite: dropping half the keyframes must leave it **green**, because a lost keyframe is
designed to cost latency and never correctness.

Its comparator forgives exactly one thing (object-identity ids, which two independent
identity maps cannot agree on by construction). Truncation and redaction are *not*
forgiven: the observer applies the same capture policy, so a truncated list must match a
*truncated* list exactly. A lenient comparator is a test that always passes and protects
nothing.

### Programs nobody wrote

The referee can only judge the programs you point it at, and those were five examples a
human thought to write. Humans write the code they already have in mind; the bugs live in
the code they do not.

So a Hypothesis grammar generates Python programs — nested functions, closures writing to
enclosing scopes, generators abandoned mid-iteration, `del` and rebinding, mutable default
arguments, `raise` inside `finally`, recursion, comprehension scopes — and hands each one
to the referee. They are **terminating and deterministic by construction**, not by
timeout: there is no `while`, loop bounds are literals, recursion carries a guarded
countdown. A timeout only tells you a program hung; a grammar that cannot express
non-termination means it never does.

Three thousand programs per property, nightly.

### What this actually caught

Not hypothetical. In order:

| found by | defect |
|---|---|
| the oracle | reconstruction was **path-dependent** — the same instant rendered differently depending on how you scrubbed to it |
| the oracle | an exception in flight across a keyframe vanished (fixed in the format) |
| the referee, first run | `del x` left the binding alive in reconstruction forever — the debugger showing a variable the program had deleted |
| the referee, on 3.12 | **two unrelated frames fused into one** when a leaked frame's address was reused |
| the campaign | seven bugs, including one the shallow profile could never assemble |

---

## What it is not

ChronoTrace is a **scrubber over a recording**. It is not an IDE, not an editor, not a
live debugger. There is no editing code, no restarting with a change, no breakpoint that
pauses a running process, and no setting a variable to continue with. Every breakpoint is
*retroactive* — evaluated against a finished recording. That is the thesis, not a
limitation.

The recording is append-only and immutable, which is exactly what makes reconstruction a
pure function of `seq`, and that in turn is what makes any of the above provable.

---

## Where to look in the code

| you want | read |
|---|---|
| the event vocabulary | `src/chronotrace/recorder/events.py` |
| why frames are a registry | [ADR-0002](adr/0002-frame-registry.md) |
| the file format | [`docs/format-spec.md`](format-spec.md) |
| the reconstruction algorithm and its cost proof | [ADR-0006](adr/0006-reconstruction.md) |
| backward stepping | `src/chronotrace/reconstruct/stepping.py` |
| why the referee is trustworthy | [`tests/equivalence/README.md`](../tests/equivalence/README.md) |
| every measured number here | [`benchmarks/RESULTS.md`](../benchmarks/RESULTS.md) |
