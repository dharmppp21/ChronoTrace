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

## Day 5 — the real recorder vs day 2's no-op floor

First measurement of the **actual** `Recorder`, not a spike callback that appends a
tuple. Median of 5, `start()`/`stop()` inside the timed region (a user pays that too).

| workload | baseline | recorder | vs base | events | ns/event |
|---|---:|---:|---:|---:|---:|
| tight_loop | 8.07 ms | 2753.61 ms | **341.2×** | 750,007 | 3661 |
| fib_recursive | 8.84 ms | 2172.84 ms | 245.9× | 600,198 | 3605 |
| json_pipeline | 15.59 ms | 1953.93 ms | **125.3×** | 195,774 | 9901 |
| io_bound | 182.45 ms | 277.20 ms | 1.5× | 457 | — |

### This is 21x above day 2's floor, and above ADR-0001's reversal trigger

Day 2 measured ~170 ns/event for a callback that appended a tuple. The real
callback costs **3661 ns/event**. ADR-0001's reversal trigger is "realistic
workloads under ~20×"; json_pipeline is at 125×. **Today the recorder is outside
its own budget.** Saying so plainly now is the point of measuring.

Three dated fixes stand between here and that trigger, and none is speculative:

1. **Day 9 — stdlib scope filtering.** json_pipeline recorded **195,774** events
   today against day 2's scoped **13,210**, because today's scope only excludes
   ChronoTrace itself; `strptime` and `statistics` are still recorded in full. A
   ~15x event reduction is already measured on this exact workload.
2. **Day 8 — value dedup / change detection.** Day 3 measured this as the
   difference between 2,370× and 6.1×.
3. **Days 40-41 — profile and optimise.** The per-event budget has obvious
   suspects, none yet measured individually: `Event` construction (827 ns, day 4),
   `time.perf_counter_ns`, `threading.get_ident`, the `threading.local` probe,
   `intern` (71 ns — code objects hash by value), and two method calls.

Day 40 is the reckoning; this row is what it will be measured against.

### Two caveats that keep the table honest

* **io_bound's 1.5× is fixed cost, not per-event cost.** 457 events cannot account
  for 95 ms. `set_events` instruments code objects across the whole process, and
  that one-time price is inside the timed region here. It is real — a user pays it
  — but it is a startup cost, not a tracing cost, and it will not grow with
  program length. Worth isolating on day 40.
* **These are not comparable to day 2's "scoped" rows.** Day 2 scoped to one
  module; today's scope only excludes ChronoTrace. The fair day-2 comparison is
  `mon_line_append` (178,917 events, 5.1×) — against which we are ~20x more
  expensive per event.

---

## Day 7 — the first honest end-to-end figure (hooks + capture)

The `capture_values` flag exists precisely so these two columns can be separated.

| workload | baseline | flow only | **+ capture** | events |
|---|---:|---:|---:|---:|
| tight_loop | 8.28 ms | 355.6× | **2577.1×** | 4,500,040 |
| fib_recursive | 8.95 ms | 278.6× | **483.8×** | 900,324 |
| json_pipeline | 16.61 ms | 127.4× | **4373.4×** | 3,168,757 |
| io_bound | 155.52 ms | 1.0× | **1.0×** | 2,294 |

**This is the planned catastrophe, not a surprise.** Day 3 measured naive capture
on json_pipeline at 2,370× and wrote down why: a 1200-element list re-walked on
every line though it never changed after line one. Today it is 4,373× — *worse*
than the spike, because day 3 scoped to a single module while today's recorder
scopes only against ChronoTrace itself, so `strptime` and `statistics` locals get
captured too. 3.17M events against 195k flow-only: 16× more, one VAR_WRITE per
local per line.

Day 3 also measured the fix: change detection took the same workload to **6.1×**.
That is day 8, and ADR-0001's whole yes rests on it.

Two things are worth stating rather than glossing:

* **This was built the slow way on purpose.** Shipping capture and dedup together
  would mean that when the combined figure disappoints there is no way to tell
  which half is at fault. `capture_values=False` isolates it from above; day 8
  isolates it from below.
* **io_bound is still 1.0×.** Instrumentation cost tracks Python lines executed,
  not wall time — the day 2 claim survives contact with real capture.

### Capture policy: the hole day 3's zoo never found

`max_depth=6` × `max_items=100` permits 100⁶ = **10¹² nodes**. Depth and item
limits bound each *dimension*; nothing bounded their product. A 20×20×20×20×20
nested list — an ordinary shape, not a contrived one — measured at:

    26,042 ms and 415,999,995 bytes   for ONE variable on ONE line

Day 3's zoo had deep-and-narrow (10k × 1) and wide-and-shallow (10M × 1) and
never wide-**and**-deep. `max_nodes=512` closes it; `tests/fixtures/hostile.py`
now carries the case so it cannot reopen.

### Day 7 micro-measurements

| question | answer |
|---|---|
| dispatch: exact-type dict | **113 ns/value** |
| dispatch: isinstance chain | 202 ns/value (1.8×) |
| dispatch: `functools.singledispatch` | 322 ns/value (2.9×) |
| frames added by recursive capture on a 10,000-deep dict | **7** |
| user stack depth at which capture raises RecursionError | ~995 of 1000 |

The last two killed the brief's demand for an iterative work stack: `max_depth`,
not the data, bounds the stack. An iterative walk would move the cliff from ~995
to ~998 — it does not solve deep-stack capture, it shifts it three frames, for
much harder code.

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
