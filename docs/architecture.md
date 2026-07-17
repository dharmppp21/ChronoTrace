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

## What exists today (Phase 1 complete)

Only `recorder` is implemented. `store` has its format **specified** but no codec
yet; the rest are names in this diagram. The recorder produces an in-memory event
stream (`MemorySink`); Phase 2 (days 11–18) makes it durable.

| Layer | Status | Built |
|---|---|---|
| recorder | **done** | days 4–10 |
| store | format spec'd ([`docs/format-spec.md`](format-spec.md)); codec next | days 11–18 |
| index, reconstruct, query, server, frontend | planned | phases 2–5 |

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
