# Spike results: line-observation overhead

**Question:** can we observe every line of a Python program at a cost a developer
will tolerate?

**Answer: yes, comfortably — but not for the reason we assumed.** Two findings
contradict the premise this project was scoped on. Both are load-bearing.

> **Read this with [RESULTS-capture.md](RESULTS-capture.md).** Every number below
> measures a callback that counts or appends a tuple. **That is not recording.**
> Day 3 added real value capture and the realistic workload went from 1.5x to
> **2,370x** — rescued to **6.1x** only by change detection. The figures here are
> a *floor*, and quoting the 1.5x as ChronoTrace's overhead would be dishonest.
> Combined figures: [RESULTS-capture.md#7-the-combined-figure--the-number-that-matters](RESULTS-capture.md).

---

## Environment

| | |
|---|---|
| CPU | 13th Gen Intel Core i5-13450HX, 10 physical / 16 logical, 2400 MHz base |
| RAM | 15.7 GB |
| OS | Windows 11 Home Single Language, build 26200 |
| Power scheme | High performance (set deliberately: a balanced profile lets the CPU downclock mid-run and inflates variance) |
| Python | 3.14.3 |
| Date | 2026-07-15 |
| Harness | `spikes/bench_overhead.py`, reps=7, one fresh subprocess per sample |

Reproduce with `python spikes/bench_overhead.py --reps 7`.

---

## Results (medians, reps=7)

| Workload | Condition | Median | vs base | LINE events |
|---|---|---:|---:|---:|
| **tight_loop** | baseline | 8.42 ms | 1.0× | 0 |
| | settrace no-op | 60.75 ms | **7.2×** | 750,004 |
| | monitoring LINE no-op | 67.54 ms | **8.0×** | 750,007 |
| | monitoring LINE + append | 104.78 ms | **12.4×** | 750,007 |
| | monitoring LINE + scoped | 132.60 ms | **15.8×** | 750,003 |
| **fib_recursive** | baseline | 4.98 ms | 1.0× | 0 |
| | settrace no-op | 53.31 ms | 10.7× | 450,147 |
| | monitoring LINE no-op | 33.71 ms | 6.8× | 300,102 |
| | monitoring LINE + append | 45.53 ms | 9.1× | 300,102 |
| | monitoring LINE + scoped | 53.93 ms | 10.8× | 300,098 |
| **json_pipeline** | baseline | 7.08 ms | 1.0× | 0 |
| | settrace no-op | 24.17 ms | 3.4× | 188,545 |
| | monitoring LINE no-op | 24.47 ms | 3.5× | 178,917 |
| | monitoring LINE + append | 35.01 ms | 4.9× | 178,917 |
| | monitoring LINE + scoped | **10.63 ms** | **1.5×** | **13,210** |
| **io_bound** | baseline | 160.50 ms | 1.0× | 0 |
| | settrace no-op | 161.19 ms | 1.0× | 454 |
| | monitoring LINE no-op | 159.89 ms | 1.0× | 457 |
| | monitoring LINE + append | 160.00 ms | 1.0× | 457 |
| | monitoring LINE + scoped | 158.83 ms | 1.0× | 453 |

---

## Finding 1: `sys.monitoring` is **not** faster than `settrace` at tracing everything

This contradicts the assumption the project was scoped on.

On `tight_loop`, where both mechanisms deliver a near-identical event count
(750,004 vs 750,007 — an apples-to-apples comparison), **`settrace` wins**:
7.2× against monitoring's 8.0×. On `json_pipeline` they tie (3.4× vs 3.5×).

Monitoring only wins on `fib_recursive` (6.8× vs 10.7×), and that comparison is
**unfair to settrace**: a settrace local tracer receives `call`, `line`, `return`
and `exception` events, so it handled 450,147 events against monitoring's 300,102
`LINE`-only. It did ~1.5× more work. Correct for that and the two are close again.

**Interpretation.** PEP 669's advantage is *not* a cheaper per-event path. When
you ask it to instrument everything, it instruments everything, and pays roughly
what settrace pays. Its advantage is **structural**: it can be told to stop.

This does not threaten the project — but it kills a lazy claim we might have made
("we're fast because we use the modern API"). The truth is more useful: we're fast
because we *scope*, and PEP 669 is the API that makes scoping possible.

## Finding 2: `DISABLE` is a 3.3× win — or a 27% loss

The scoping lever is not universally good, and that surprised me.

| Workload | append | scoped | effect |
|---|---:|---:|---|
| json_pipeline | 4.9× | **1.5×** | **3.3× faster**, 93% fewer events (178,917 → 13,210) |
| tight_loop | 12.4× | 15.8× | **27% slower** |
| fib_recursive | 9.1× | 10.8× | 19% slower |

**Why.** `tight_loop` and `fib_recursive` live entirely inside `workloads.py`, so
every line is *in scope*: the callback disables nothing and the
`code.co_filename != target` string comparison is pure added cost. Across 750,003
events that overhead is ~28 ms, i.e. **~37 ns per event just to ask "is this mine?"**

`json_pipeline` calls pure-Python stdlib (`strptime`, `statistics`), so 93% of its
LINE events are out of scope. Each of those locations costs exactly *one* callback,
ever — then de-instruments itself. That is the asymptotic change the whole design
wants.

**Design consequence for day 9 — the scope check must not be per-event.** Today's
naive per-event `co_filename` compare is measurably a loss on in-scope-heavy code.
Two ways out, both worth trying:

1. **Cache the decision per code object.** A code object's scope never changes, so
   the string compare should happen once per code object, not once per line. Day 9
   already plans this; this measurement makes it mandatory rather than nice.
2. **Invert the mechanism: `set_local_events` instead of global `set_events` +
   DISABLE.** Discover code objects via `PY_START`, then instrument *only* in-scope
   ones. In-scope code then pays no check at all, because out-of-scope code was
   never instrumented in the first place. This is strictly better in principle and
   deserves its own spike before day 9 commits.

## Finding 3: I/O-bound work is free (as predicted)

All conditions land at 1.0× on `io_bound` — 454 events across 160 ms. Instrumentation
cost tracks **Python lines executed**, not wall time. A program blocked on I/O
executes almost no bytecode, so there is almost nothing to instrument. The control
worked: the claim is now measured rather than asserted.

---

## Verdict: tolerable, with room to spare

The honest comparison is against what people already accept. `pdb` under `settrace`
is widely cited at 50–100× and developers use it daily.

- **Realistic workload, scoped: 1.5×.** This is not "tolerable", it is *good*.
- **Worst case (tight numeric loop): 12–16×.** Still 3–6× better than pdb.
- **I/O-bound: free.**

These numbers are better than the project's own planning assumed, with one caveat
that keeps them honest: **our callbacks are trivial.** They count or append a
tuple. Real recording adds value capture, hashing, dedup and serialisation — day 3
measures that, and the combined figure is the one that matters. Today's number is
the *floor*, not the answer.

**Escape hatches if a user hits the tight-loop case:** narrower `--include` scope,
or the `--sample` mode sketched for day 42 (record every Nth line), which changes
the asymptotics rather than the constant.

---

## Methodology, and where it is weak

**What was done right:**

- **One fresh subprocess per sample.** `sys.monitoring` instrumentation is
  process-global and sticky: `DISABLE` permanently de-instruments a location until
  `restart_events()`, which re-enables *everything for every tool*. Running the
  scoped condition before the no-op condition in one process would have handed the
  no-op condition a partially de-instrumented interpreter and produced impossibly
  good numbers. This is the mistake that would have invalidated the whole spike.
- **Median and p95, never best-of-N.** Best-of-N reports the luckiest scheduling
  accident on the machine.
- **Event counts asserted non-zero** (`tests/test_spike_harness.py`). A callback
  that silently never fires produces beautiful overhead numbers and measures
  nothing. This is the classic way to fool yourself, so it is tested.
- **Warmup is uninstrumented on purpose.** A real recording gets no warmup either.
- **Determinism verified:** event counts were bit-identical across all three full
  runs (750,007 and 13,210 every time).

**Where it is weak — stated rather than buried:**

1. **GC is disabled around the timed region.** This buys stability and costs
   realism, and it **understates the `append` condition specifically**: appending
   750k tuples is exactly the allocation pressure that would trigger collection.
   Real overhead for value-capturing conditions is therefore *higher* than shown.
   Day 3 must measure with GC enabled.
2. **Stability missed the 10% bar in one cell.** Across three full runs, most
   medians held within ~3–5%, but `json_pipeline / scoped` ranged 9.63–10.99 ms
   (~14%). It is a ~10 ms measurement, so timer and scheduler noise are a larger
   fraction of it. The derived ratio (1.5×) is stable; the absolute is soft.
3. **settrace vs monitoring is not perfectly matched** on event count (settrace
   receives call/return/exception too). Only the `tight_loop` row is a clean
   comparison, and that is the row Finding 1 rests on.
4. **Single machine, single OS.** Windows only. Linux and macOS may differ,
   particularly on timer resolution and scheduler behaviour. Day 43's benchmark
   suite runs the matrix.

---

## Open questions this spike raised

- Does `set_local_events` on discovered code objects beat global `set_events` +
  `DISABLE`? (Finding 2. Worth a spike before day 9.)
- What is the combined overhead once value capture is added? (Day 3.)
- How much does GC add back for the append-heavy conditions? (Day 3.)
