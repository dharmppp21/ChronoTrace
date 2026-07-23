# Architecture

ChronoTrace is seven layers with a strict one-way dependency rule. Each layer may
use the layers below it and knows nothing of the layers above.

```
  target.py
     │  (imported into the target's own process)
     ▼
┌──────────┐   records execution via PEP 669 sys.monitoring
│ recorder │   → lines, calls, returns, exceptions, local values
└────┬─────┘
     ▼
┌──────────┐   the .chrono file format: framed, checksummed,
│  store   │   zstd-compressed columns, keyframes + deltas, mmap
└────┬─────┘
     ▼
┌──────────┐   secondary indexes over the store: by seq, by
│  index   │   variable, the call tree (entry_seq / exit_seq)
└────┬─────┘
     ▼
┌──────────────┐  reach any past instant: nearest keyframe +
│ reconstruct  │  a bounded number of deltas → O(log N) scrubbing
└────┬─────────┘
     ▼
┌──────────┐   causal questions: "who last wrote to `total`?",
│  query   │   "where did this exception originate?"
└────┬─────┘
     ▼
┌──────────┐   HTTP/WebSocket API over a recording
│  server  │
└────┬─────┘
     ▼
┌──────────┐   timeline UI: drag a playhead, watch state change
│ frontend │
└──────────┘
```

## The one-way rule

**Dependencies point down only:** `server → query → reconstruct → index → store →
recorder`. No layer imports a layer above it. `config` and `cli` sit above every
layer (they wire the application together) and may import anything.

This is not a convention we hope to remember -- it is enforced by
[`tests/test_architecture.py`](../tests/test_architecture.py), which parses every
source file's imports and fails the build on any upward reference. The rule holds
even for layers that do not exist yet: the moment `store` is written and imports
`query`, the test goes red.

### Why it matters most for the recorder

The recorder is imported **into the process being debugged**. Every module it
imports becomes a module in the user's program, another possible version clash,
another line the scope filter must exclude. If the recorder imported `store`, the
file format and its compression dependency would be dragged into every recorded
program. So the recorder is the bottom of the order and depends on nothing above
it -- and the store is allowed to import the recorder's *event model* only, never
its runtime machinery (the monitoring callbacks, the frame registry), so the file
format stays free to change independently of how observation works.

## What exists today

`recorder`, `store`, `reconstruct` and `index` are implemented: a program can be recorded,
persisted durably, scrubbed backward instant by instant, and asked timeline-wide questions.
`query` has begun — a typed API with its first two queries — and the layers above it are
names in this diagram.

| Layer | Status | Built |
|---|---|---|
| recorder | **done** | days 4–10 |
| store | **done (M2, day 18)** — writer + reader + zstd + value pool + keyframes + deltas + crash recovery, defaults tuned by grid ([`docs/format-spec.md`](format-spec.md), [ADR-0005](adr/0005-storage-defaults.md)) | days 11–18 |
| reconstruct | **done (M3, day 24)** — `reconstruct(seq)` in O(log K), backward stepping, replay-equivalence referee ([ADR-0006](adr/0006-reconstruction.md)) | days 19–24 |
| index | **built** ([ADR-0008](adr/0008-index-schema.md)) — one-pass driver, five indexers: var writes, line hits, call tree, exceptions, density | days 25–27 |
| query | **begun** — typed API, injected context, cursor pagination, a stated latency contract; `var-writes` and `line-hits` queries | days 28–31 |
| server, frontend | planned | phases 4–5 |

## The storage format

The `store` layer persists the event stream to the `.chrono` file format —
a header, length-and-CRC-framed blocks, and an index written last whose presence
signals a clean close. Events are **columnar** (measured 7–12× smaller than row,
[ADR-0004](adr/0004-chrono-file-format.md)); values are a content-addressed pool;
opening a file is a pure data operation with `pickle` banned at the spec level.
The normative byte layout — implementable from scratch in any language — is
[`docs/format-spec.md`](format-spec.md); its machine form is
`src/chronotrace/store/constants.py`. The store may import the recorder's *event
model* only, never its runtime machinery, so the file format stays free to change
independently of how observation works.

### The codec: keyframe → deltas → any instant

Reaching a past instant is a video codec. A **keyframe** (every N events) is the full
live state at an instant; **deltas** are exactly what changed between them. Any `seq`
is then the nearest keyframe plus a bounded replay — and because deltas store the *old*
ref as well as the new, a step backward is one delta inverted, not a rewind.

```
  keyframes:   K0 ─────────── K1 ─────────── K2 ─────────── K3      (every N events)
                │              │              │              │
  deltas:       ·δδδ·δ·δδ····δ·│δ·δδ·····δδ·δ·│···δδ·δ····δ·δ│δ··    (bind / enter / exit)
                              ▲
  reach seq S:  nearest keyframe at or before S  (O(log K))
                + apply the deltas from there to S   (at most N — the latency contract)
  step back:    invert one delta                     (O(1), never a keyframe rewind)
```

`store/keyframe.py` owns the snapshot and the shared state a delta mutates;
`store/delta.py` owns the delta and the two pure functions `apply` (forward) and
`invert` (backward), whose referee is the property `invert(apply(s, d)) == s`.
Reconstruction (day 21) is the layer that drives this codec to answer "state at S".

## The index: making the past queryable

Reconstruction answers one instant at a time. Debugging asks timeline-wide questions —
*who last wrote to `total`?*, *where did this exception originate?* — which cost
O(events) each to replay. The `index` layer precomputes them into a **SQLite sidecar**
(`recording.chrono.idx`), turning each into a B-tree lookup: the demo query, "the last
write to `x` before `seq`", measures **37 µs**.

It is **derived state and never authoritative**: every fact comes from the `.chrono`, so
the index can be deleted at any time, rebuilt from the recording alone, and is discarded
rather than trusted when a stamped fingerprint says the recording changed. It stores
**pointers, not events** — the events stay in the `.chrono` — which is what keeps it to
14.5 bytes per event. That is still three times the size of the recording, because a
B-tree of pointers is uncompressed while the recording is columnar and zstd'd; the
tradeoff, the timing decision, and the 10 GB consequence are all in
[ADR-0008](adr/0008-index-schema.md).

## The query engine: instants you can jump to

The index makes the past *lookup-able*; the `query` layer makes it *interrogable*. Its one
design idea, and the line the whole layer is built around: **a query result is a set of
instants you can jump to.** A query that returns text is a `grep`; a query that returns
`seq` numbers — the universal address every layer already speaks — is a debugger, because
each result is a place you can land and inspect. Every query is a typed callable
(`VarWritesQuery(name="total")`) with a typed result, run against an injected `QueryContext`
that owns the recording and the index connection so they are opened once and closed once —
and so a query can be handed a *fake* context in a test, which is the proof the injection is
real rather than decorative. The full catalogue is [`docs/queries.md`](queries.md).

Three decisions carry the layer, each argued in `src/chronotrace/query/types.py`:

- **A typed API, not a query DSL.** A language needs a parser, a grammar, error messages,
  documentation and a stability promise — all before anyone knows which queries matter. The
  type checker is the parser instead. A DSL is revisited only when a real user is composing
  queries by hand, repeatedly, because the API cannot express what they mean
  ([#13](https://github.com/dharmppp21/ChronoTrace/issues/13)).
- **Cursor pagination, never an unbounded list.** "Every write to `i`" in a hot loop is ten
  million rows. Results come one page at a time, and the cursor is a `seq` (`WHERE seq > ?`)
  rather than an `OFFSET`: an `OFFSET` re-reads and discards every earlier row, so paging to
  the end is O(rows²), while a `seq` cursor costs the same on page one and page ten thousand
  and — being unique and monotonic — can neither skip a row nor return one twice.
- **A latency contract that is asserted, not hoped for.** p95 < 50 ms on a ten-million-event
  recording, checked by a test that also inspects the query plan, so a budget met by a full
  scan cannot pass. The clustered index keeps a page query at O(log n + page).

Results are honest about their edges: a partial (crash-truncated) recording flags every
result `partial` rather than silently under-reporting, and a typo (`no such variable`, `no
such file`) is a different, louder answer than a valid query that found nothing.

## How correctness is established

Three layers of proof, each catching what the one below cannot. They are part of the
design, not an afterthought bolted on at test time.

| proof | what it compares | what it cannot catch |
|---|---|---|
| unit tests | each piece against hand-written expectations | pieces that are individually right and wrong together |
| the **oracle** (day 20, `reconstruct/oracle.py`) | the fast reconstruction against a slow, obviously-correct one that ships forever | a *recorder* that misunderstood the program — both paths are then wrong together |
| the **referee** (day 22, `tests/equivalence/`) | reconstructed state against the state the program actually had, observed live | a bug inside `capture` itself, which both observers share by necessity |
| the **campaign** (day 23, `tests/property/`) | the referee's verdict over thousands of machine-generated programs | constructs the grammar cannot express (threads, `eval`, `async`) |

The campaign exists because the referee can only judge the programs it is pointed at, and
those were five examples a human thought to write. A Hypothesis grammar generates valid,
**terminating and deterministic** Python — bounded structurally rather than by a timeout,
because a timeout only tells you a program hung. Its storage parameters are drawn too:
the first clean campaign turned out to average 0.3 keyframes per program, so the keyframe
and delta machinery was barely under test until `keyframe_interval` became an input.

The referee is the only one that spans all five subsystems at once — recorder, store,
keyframes, deltas, reconstructor. Its ground truth comes from a **second
`sys.monitoring` tool** under its own tool id, reading `frame.f_locals` directly and
importing none of the recorder's machinery. That independence is the whole point: a truth
source built from `FrameRegistry` agrees with `FrameRegistry`'s bugs, and the test would
assert `X == X` forever while shipping a debugger that lies.

Two properties make it trustworthy rather than decorative:

- **It is proven to fail.** Four bugs are deliberately injected — a dropped delta, a
  lying keyframe, content-blind dedup (the day-8 mutation bug), a drifting reconstruction
  cache — and it must go red for each.
- **Its comparator is not lenient.** Exactly one difference is forgiven (object-identity
  ids, which two independent identity maps cannot agree on by construction). Truncation
  and redaction are *not* allowances: the observer applies the same policy, so a truncated
  value must match a truncated value exactly.

It caught a real, untracked defect on its first run — `del x` leaves a binding alive in
reconstruction forever ([#7](https://github.com/dharmppp21/ChronoTrace/issues/7)).
A red referee blocks merge; see [CONTRIBUTING](../CONTRIBUTING.md#the-referee) and
[`tests/equivalence/README.md`](../tests/equivalence/README.md).

## Recorder internals

Within the recorder, the pieces and their jobs (all under
`src/chronotrace/recorder/`):

- **`events`** — the event model: a frozen `seq`-addressed record, interning, the `Sink` protocol.
- **`recorder`** — wires `sys.monitoring` callbacks to the event model; the only code in the user's hot path.
- **`frames`** — the live-frame registry ([ADR-0002](adr/0002-frame-registry.md)): stable `frame_id`s across suspend/resume.
- **`capture`** — turns any object into bounded, cycle-safe data without invoking user code or retaining it.
- **`identity`** — weak monotonic object ids for the UI's aliasing badges, holding no user object alive.
- **`dedup` + `values`** — content-addressed value deduplication ([ADR-0003](adr/0003-dedup-correctness.md)).
- **`scope`** — decides what is "my code"; returns `DISABLE` for the stdlib so CPython stops calling us.
- **`redact`** — withholds secret-named locals *before* they are read into our buffers.
