# ADR-0001: Omniscient recording, not deterministic replay

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** dharmppp21
- **Supersedes / Superseded by:** none

## Context

ChronoTrace must let a developer reach any past instant of a program's execution.
Two families of designs do that, and the choice determines the storage format, the
query engine, and whether random access is possible at all. It is effectively
irreversible: everything in phases 2–5 is built on top of it.

Days 2 and 3 measured, rather than assumed, what each would cost.

### Measured: line observation (day 2, `spikes/RESULTS-overhead.md`)

i5-13450HX, Windows 11, Python 3.14.3, medians of 5–7 reps, one fresh subprocess
per sample.

Two findings contradicted our own assumptions:

1. **`sys.monitoring` is not inherently faster than `sys.settrace`.** On the one
   cleanly-matched comparison (`tight_loop`, near-identical event counts) settrace
   *won*, 7.2× vs 8.0×. PEP 669's advantage is structural — it can be told to stop
   via `DISABLE` — not a cheaper per-event path. "We're fast because we use the
   modern API" would have been false.
2. **Scoping is worth 3.3× on realistic code and *costs* 19–27% on code that is
   entirely in scope**, where the per-event `co_filename` check (~37 ns) buys
   nothing. The scope decision must be cached per code object.

### Measured: value capture (day 3, `spikes/RESULTS-capture.md`)

**Safety is settled.** Capture never invokes user code, never retains objects, and
stays under 4 KB against a hostile zoo (cycles, 10M lists, depth-10k dicts, a
raising `__repr__`, a side-effecting property, sockets, locks, a 4 GB fake buffer).
Every claim is a passing test, not an assertion. `reprlib` was evaluated first and
rejected — it *ran* the user's `__repr__` and returns strings, which cannot be
expanded or diffed.

**Cost is the problem.** Day 2's numbers were the floor: a callback appending a
tuple is not recording. The combined figure:

| workload | baseline | scoped only | **+ naive capture** | **+ change detection** |
|---|---:|---:|---:|---:|
| tight_loop | 8.29 ms | 16.4× | **123.3×** | 107.3× |
| fib_recursive | 5.43 ms | 10.0× | **35.7×** | 32.9× |
| **json_pipeline** (realistic) | 7.08 ms | 1.3× | **2,370.5×** | **6.1×** |
| io_bound | 159.37 ms | 1.0× | 1.0× | 1.0× |

Naive omniscient capture is **2,370×** — a 6.88 ms program takes 16.5 seconds.
That would have ended the project. Change detection takes it to **6.1×**: a 387×
improvement, and the single reason this ADR can say yes.

**The catch, stated plainly:** the change detection measured above uses object
identity as a proxy for "unchanged", which is **unsound** — `lst.append(x)` keeps
the same `id`. Day 8's sound rule (identity shortcut for immutables only) does not
obviously rescue `json_pipeline`, whose hot locals are mutable lists. The honest
figure lives somewhere between 6.1× and 2,370×.

## Decision

**ChronoTrace records omnisciently: it captures state as execution happens and
writes it all down.** We do not record only nondeterministic inputs and re-execute.

## Alternatives considered

### Deterministic replay (the `rr` approach)

Record only sources of nondeterminism, then re-execute the program to reach a past
instant. Tiny recordings, low overhead — genuinely attractive at 6.1×–107×.

**Rejected for two reasons, in order of weight:**

1. **It does not give random access, which is the entire product.** Reaching event
   500,000 means re-executing 500,000 events. Dragging a timeline scrubber
   backwards would re-run the program on every mouse-move. The feature ChronoTrace
   exists for — *scrub the past like video* — is not implementable on top of
   replay without also building the omniscient layer you were trying to avoid.
2. **In pure Python it is a research project, not a phase.** You must intercept
   *every* source of nondeterminism: `time`, `random`, all I/O, threads and their
   scheduling, hash seeds, dict iteration order, `id()` values, and every C
   extension — which can do anything and tell you nothing. Miss one and replay
   silently diverges, so the debugger confidently shows a past that never
   happened. `rr` achieves this by controlling the kernel interface; we would be
   chasing an unbounded surface inside the interpreter with no way to know we had
   covered it.

The failure mode is what decides it: an omniscient recording that is too big is a
*visible, annoying* problem. A replay that diverges is an *invisible, confident
lie* — the worst possible failure for a debugging tool.

### Hybrid: replay with periodic snapshots

Snapshot occasionally, replay forward from the nearest. Bounded re-execution,
smaller than full capture.

**Rejected:** it inherits replay's fatal requirement (intercept all
nondeterminism) while adding snapshotting, so it is strictly more work than the
option we chose, for a storage win we do not need. Storage is cheap; developer
time is not. Worth revisiting only if recordings prove unmanageably large in
practice — which is a measurable trigger, not a guess.

### Sampling (record every Nth line)

**Not rejected — deferred.** It is not a substitute for omniscient recording, it is
an escape hatch *within* it, for the tight-loop case (107×). Day 42 revisits it as
`--sample`. Recorded here so nobody mistakes it for a road not taken.

## Consequences

**What this buys us**

- Random access by construction. Any past instant is reachable without
  re-execution, which is what makes the scrubber, backward stepping and
  retroactive breakpoints possible at all.
- No divergence risk. What we show happened, because we watched it happen.
- The hard problem becomes *engineering* (compress harder, capture less) rather
  than *research* (enumerate all nondeterminism in CPython).

**What this costs us**

- **Recordings are large.** Phase 2 must compress hard — this is why days 11–18
  exist, and why keyframes + deltas (a video codec, not a log) is the storage model.
- **Overhead is real**: ~6× realistic, ~107× tight loops. Honest comparison: `pdb`
  is widely cited at 50–100× and people use it daily. Tolerable, not free. It goes
  in the README as a number, not a boast.
- **Recordings contain the program's memory** — credentials, tokens, PII. A
  `.chrono` file is as sensitive as a core dump. This forces the day 47 threat
  model, the pre-capture redaction on day 9, and the ban on any network egress.
- **Capture is lossy by policy** (depth 6, 100 items, 512 chars). The loss must be
  *visible in the UI*, never silent. A tool that shows 100 of 10,000,000 items
  without saying so teaches the user to distrust everything else it shows.
- **`pickle` is banned at the spec level** (day 11). An 163-byte malicious
  recording executed code merely by being opened. Recordings get shared in bug
  reports; opening a stranger's file is the normal workflow.

**What this forces later**

- Day 7–8 must solve **sound change detection**. This is the primary risk, not a
  detail: without it the architecture is 2,370× and dead. The identity shortcut is
  unsound for mutables; content hashing pays a capture before learning it was a
  duplicate. Tracked as the phase-1 risk.
- Day 9 must cache the scope decision **per code object** — day 2 measured that a
  per-event check costs 19–27% on in-scope-heavy code.
- Phase 2's format is not negotiable: it must compress aggressively and support
  keyframes + deltas, because that is the only way random access survives the file
  sizes this decision creates.

**Reversal trigger**

Revisit if **either**:

- sound change detection cannot get realistic workloads under ~20×, *and* users
  report overhead as blocking — at which point the hybrid snapshot+replay design
  earns a real re-examination; or
- recordings routinely exceed what a developer will keep on disk (tracked from day
  18's size benchmarks), *and* compression has been exhausted.

Neither is speculative — both are measurable, and both have a day that measures them.
