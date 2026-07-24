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
        yield i  # leaves the frame without exiting it
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

## 3b. Making the past queryable

Reconstruction answers *"what was the state at instant S?"* — one instant at a time. But
the questions debugging is actually made of are about the whole timeline:

> *Who last wrote to `total`?*

Answering that by replay is O(events) per question. So after recording finishes, a pass
over the event stream builds a **SQLite sidecar** (`recording.chrono.idx`) — and that
question becomes a B-tree seek:

```sql
SELECT seq, frame_id, value_ref FROM var_writes
WHERE name_id = ? AND seq < ?
ORDER BY seq DESC LIMIT 1
```

**O(log n), measured at 91 µs**, and it does not care whether the variable was written
twice or a million times. The table is clustered on `(name_id, seq)`, so the rows for one
name are contiguous *and already in order*: the query is a descending range scan that
stops at the first row it finds.

Three things about it are worth more than the SQL:

**It stores pointers, not events.** Rows hold `seq` numbers; the events stay in the
`.chrono`. That is what keeps the index to a few bytes per event instead of a second copy
of the recording.

**It is derived state and never authoritative.** Every fact comes from the recording, so
the index can be deleted at any time, rebuilt from the recording alone, and is discarded
rather than trusted when a stamped fingerprint says the recording changed. That decision
pays for itself immediately: durability can be turned off during the build (a crash means
a rebuild, not data loss), which is a large part of why it loads at ~240,000 events/s.

**`frame_id` is on every row, because of recursion.** `total` in one invocation and
`total` in another are different variables that happen to share a name. Keyed on the name
alone, "the last write to `total`" would answer with some *other* call's write —
confidently, and wrongly.

Indexing happens *after* recording, never during: the recorder's hot path is the one thing
the whole design protects. The honest consequence is that a crash-truncated recording has
no index — and that is exactly the recording most worth querying — so the first query
builds one from the recovered prefix. ([ADR-0008](adr/0008-index-schema.md).)

## 3c. Causal queries: from *what* to *why*

The index turns "what was the state at S?" into "when did X happen?". The **query engine**
turns that into the questions a debugger exists to answer — and the answer to every one is
a set of **instants you can jump to**, never text. Two of them do things no other Python
tool does.

**"Where did this exception really come from?"** A traceback shows you the frames an
exception crossed and the line it surfaced on. It does *not* show the program state at the
moment it was born, and for a chained exception it prints the earlier exception's message
but cannot take you to *its* birthplace with *its* locals. ChronoTrace does both. It walks
to the birth (the recorder marks only the first of the RAISE events CPython fires in every
crossed frame), then follows the recorded `__cause__` / `__context__` links to the root:

```
RuntimeError  born at [25]  <- the direct cause: KeyError born at [19]  (the root)
```

Recording those links required going back into the recorder ([#11]): at each raise, it maps
the exception's identity to the instant it was born, so a later `raise X from Y` can point X
back at Y. The link is a *recorded fact*, not a guess from adjacency — which matters,
because the earlier exception is often already marked handled by the time the next one is
raised, so nothing on the timeline's surface connects them.

**"Where did this value come from?"** This is where honesty is the whole design. ChronoTrace
records writes, not reads (recording every read would multiply event volume for a question
nobody asks). So the exact, always-correct answer is *the write that produced this value*
and the state there — the inputs are on screen for a human to read. On top of that comes a
*heuristic*: parse the writing line's source, find the names it reads, resolve each to its
own last write. That guess is presented as **"likely inputs", never "the cause"**, and it
**refuses to run against a source file that changed since the recording** — each file's
SHA-256 was stored at record time, and a mismatch makes the heuristic decline rather than
confidently describe a line that no longer exists.

That refusal is the rule the whole engine is built on: *an approximation shown honestly is a
feature; an approximation shown as truth is a lie.* It is why the demo bug —
`totals = dict.fromkeys(...)`, a dict aliased under three keys ~360 events before any wrong
number prints — is solved by a single provenance query that lands on the initialisation
line, not by stepping forward past a hundred correct-looking `+=`s. ([queries.md](queries.md).)

[#11]: https://github.com/dharmppp21/ChronoTrace/issues/11

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
