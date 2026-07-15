# Benchmark results

Every number the project claims lives here. Re-run with `python benchmarks/<file>.py`.

Machine: i5-13450HX (10c/16t), 15.7 GB, Windows 11 build 26200, High performance
power scheme, Python 3.14.3.

Spike results live separately: [`spikes/RESULTS-overhead.md`](../spikes/RESULTS-overhead.md)
(line-observation cost) and [`spikes/RESULTS-capture.md`](../spikes/RESULTS-capture.md)
(value capture + the combined figure).

---

## Day 4 — event representation (`bench_events.py`)

1M events, 7 integer fields. Median of 3 for time; a **separate pass** for memory.

| design | ns/event | B/event | MB @ 1M |
|---|---:|---:|---:|
| **AoS `dataclass(slots=True, frozen=True)` — SHIPPED** | 827 | **151.3** | 151.3 |
| AoS `dataclass` (no slots) | 1319 | 191.3 | 191.3 |
| AoS `NamedTuple` | **682** | 175.3 | 175.3 |
| AoS plain tuple | **262** | 167.3 | 167.3 |
| SoA `array.array('q')` | 1028 | **57.3** | 57.3 |

### Decision: AoS `dataclass(slots=True, frozen=True)`

Nothing dominates, so the trade is explicit:

- **`slots=True` earns itself outright**: 151 B against 191 B and 827 ns against
  1319 ns. Strictly better on both axes. No argument to have.
- **NamedTuple is 18% faster and 16% hungrier.** Rejected on a correctness
  argument rather than the numbers: a NamedTuple *is* a tuple, so `ev[3]`,
  `ev + other` and iteration all work. Across seven layers and ten weeks, some
  code will index by position, and then **field order becomes public API** —
  reorderable only by breaking callers silently. A frozen slots dataclass makes
  `ev[3]` a `TypeError` at the first attempt.
- **Plain tuple is 3.2x faster (262 ns) and was rejected hardest.** Every field in
  this model is an `int`. `ev[3]` is `thread_id`, `ev[4]` is `frame_id`; swap them
  and nothing raises, no test fails, and the call tree is quietly wrong forever.
  The type checker is the only thing standing between us and that, and 565 ns/event
  is a fair price for it.
- **SoA is 2.6x smaller and was rejected as premature.** Memory is what Phase 2
  exists to fix — day 10 records 1M events into `MemorySink` and asserts a ceiling
  it is *expected* to strain, and that failure is the written argument for the file
  store. Optimising it now, before the recorder that generates the events exists,
  is exactly the speculative optimisation the project bans.

**Reversal trigger, so this is a decision and not a preference:** if day 40's
profile shows event construction above **10%** of recorder time, switch `emit` to
take fields and let the sink hold columns. The measurement is already here, so day
40 does not have to re-derive it. Note SoA is the natural shape for day 12's
columnar encoder anyway, so the change would be a convergence, not a rewrite.

### Methodology: the trap that nearly chose wrong

`tracemalloc` instruments **every allocation**, so it taxes AoS (1M objects) far
more than SoA (zero). Measured together in one pass, it reported **SoA as 3.3x
faster than AoS**. Measured apart, **AoS is faster**.

That one methodology error would have chosen the wrong representation for the
entire project. Time and memory are measured in separate passes, and the reason is
in the benchmark's docstring so it survives being forgotten.

---

## Day 4 — interning (`test_interning.py`, ad-hoc probe)

| operation | ns | note |
|---|---:|---:|
| `hash(code_object)` | 71 | code objects hash by **value** (over bytecode), not identity |
| `hash((co_filename, co_qualname))` | 60 | building the tuple still beats hashing the code |

`hash(code)` being *slower* than hashing a two-string tuple was a surprise and is
worth carrying into day 9: the per-event scope check will want the same dict
lookup, so one cache should serve both the scope decision and the `code_id`.

---

## Standing budgets

| thing | budget | current | measured |
|---|---:|---:|---|
| 1M events in `MemorySink` | day 10 sets it | 151 MB | day 4 |
| Recorder overhead, realistic workload | < 20x (ADR-0001 reversal trigger) | 6.1x* | day 3 |
| Recorder overhead, tight loop | — | 107x | day 3 |

\* with change detection whose sound version is unsolved — see
[`spikes/RESULTS-capture.md`](../spikes/RESULTS-capture.md). 6.1x is a ceiling, not
a promise.
