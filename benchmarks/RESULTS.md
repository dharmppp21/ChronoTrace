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

## Day 8 — dedup + change detection (`bench_dedup.py`)

Content-addressed deduplication (store each distinct value once) plus change
detection (emit a VAR_WRITE only when a binding's value reference actually
changed). Both are the same idea — record deltas, not restatements — and both are
measured here. Accounting is one deterministic run; overhead is median/p95 of 5.

### Recording size — the headline

**dedup + change detection cut recording size by 97.9% on the realistic workload
(json_pipeline) and 84% on the tight loop.**

| workload | captures | VAR_WRITE emitted | hit rate | distinct stored | **size cut** |
|---|---:|---:|---:|---:|---:|
| tight_loop | 3,750,013 | 600,008 | 92.4% | 286,371 | **84.2%** |
| fib_recursive | 300,106 | 150,055 | 100.0% | **31** | 50.3% |
| json_pipeline | 3,220,015 | 98,261 | 99.6% | 14,036 | **97.9%** |
| io_bound | 1,817 | 309 | 91.3% | 158 | 78.0% |

*captures* = every (local, line) fed to the pool. *emitted* = VAR_WRITE events
that survived change detection. *hit rate* = fraction of captures whose content
was already pooled. *size* = `events x 151 B` (day 4) `+ serialised value bytes`,
naive (day 7: one event and one stored value per capture) against now.

* **json_pipeline is the win.** A 1200-record parse re-reads the same
  `records`/`parsed` lists on every line; 3.22M captures collapse to 14,036
  distinct values (99.6% hit rate), and change detection drops 3.22M would-be
  VAR_WRITEs to 98,261. 97.9% smaller.
* **fib_recursive dedups perfectly (31 distinct values) yet only shrinks 50%.**
  Its frames read `n` once and return, so change detection has almost nothing to
  suppress — the events are already minimal; only storage dedups. A workload
  where each local is read once is the case change detection cannot help.

### Overhead — size fell far more than time, and that is the honest result

| workload | day 7 (+capture) | **day 8 median** | day 8 p95 |
|---|---:|---:|---:|
| tight_loop | 2577.1x | **1268.2x** | 1327.1x |
| fib_recursive | 483.8x | **440.1x** | 465.1x |
| json_pipeline | 4373.4x | **2858.9x** | 128845.8x † |
| io_bound | 1.0x | **1.0x** | 1.1x |

Size fell 84–98%; time fell only ~1.5–2x. That gap is the finding, not a
disappointment: **change detection deduplicates *emission and storage*, but the
`capture()` + hash still runs on every local every line** — 3.22M times on
json_pipeline — because a mutable object could have changed and only a re-capture
can prove it didn't. Emission and storage were never the floor; the per-line
capture is. Cutting the *number* of captures is day 9's job — scope filtering
stops capturing `strptime`/`statistics` locals at all, which is where
json_pipeline's 3.22M captures actually come from.

**Day 3's 6.1x was a scoped spike, and this is why it is not repeated here.** That
number captured one module's locals; unscoped, json_pipeline is 2858.9x. Day 8
was never going to reach 6.1x soundly — that needs either the `id()` shortcut this
day rejected as unsound (see `dedup.py`) or day 9's scoping. The standing-budget
footnote is corrected below to stop implying day 8 delivers 6.1x.

† **The p95 is a gen-2 GC pause, and it is real signal.** One of five
json_pipeline runs took ~45x the median because the recorder mints *millions* of
short-lived capture dicts (3.22M here), and a full collection mid-recording adds
seconds. The median is robust to it (middle of five); the p95 is not, and it is
left in rather than hidden because it flags a day-40 question: whether to
`gc.freeze()` the target's heap or throttle collection while recording. A user
whose 16 ms program occasionally pauses 2 s will notice.

---

## Day 9 — scope filtering via DISABLE (`bench_scope.py`)

Returning `sys.monitoring.DISABLE` for out-of-scope code so CPython stops calling
us there. Measured flow-only (capture off) so the number is the *scoping* win, not
the capture cost day 8 already showed dominates. Each workload is run under the
shipped narrow scope (record the project tree, exclude stdlib + site-packages) and
under a wide scope (everything but ChronoTrace, day 8's behaviour).

| workload | wide x | wide events | **narrow x** | **narrow events** | event cut |
|---|---:|---:|---:|---:|---:|
| tight_loop | 103.1x | 750,007 | 102.5x | 750,007 | 0.0% |
| fib_recursive | 285.5x | 600,198 | 315.7x | 600,198 | 0.0% |
| **json_pipeline** | **179.8x** | **195,776** | **5.4x** | **13,214** | **93.3%** |
| io_bound | 1.0x | 457 | 1.0x | 457 | 0.0% |

**json_pipeline goes 179.8x -> 5.4x, and its flow event count 195,776 -> 13,214.**
That is a 33x overhead cut on the realistic workload, and it drops the flow stream
*under* ADR-0001's 20x reversal trigger. It comes entirely from `DISABLE`:
`strptime` and `statistics` run thousands of pure-Python lines that are no longer
recorded.

**The pure-Python loops are unchanged, and that is the control.** `tight_loop`,
`fib` and `io_bound` have nothing out of scope, so scoping cannot and does not help
them (0.0% cut; the fib row's 285.5 vs 315.7 is run-to-run noise -- identical event
counts). Scoping is not a universal speedup; it removes the stdlib, which is where
real programs -- and only real programs -- spend out-of-scope time. A benchmark
that claimed a tight-loop speedup from scoping would be measuring noise.

### DISABLE stops the call, not just the event

`test_scope_filter.py::test_disable_stops_the_callback_for_out_of_scope_code`
counts callback invocations: a 3-line function called 50 times fires the LINE
callback >= 50x in scope and <= 4x out of scope (each line's location returns
DISABLE on first sight and is never called again). Asserted directly, because
"cheap per event" and "no event" are different claims and only the second is the
point.

### Capture is now the floor -- a day-40 problem, not scope's

With value capture on, scoped `json_pipeline` still takes ~35 s per run: scoping
cut its captures 3,220,015 -> 55,240 (58x fewer), but the survivors are the
expensive ones -- `records` (1,200 dicts) and `parsed`, re-captured every loop
line. Measured, so day 40 has a target and not a hunch:

| operation (a 1,200-dict list, bounded to 512 nodes) | cost |
|---|---:|
| `capture()` | 827 us |
| `digest()` (repr + blake2b) | 249 us |
| of which `repr()` | 207 us |

`capture()` at ~1.6 us/node is the whole cost; identity assignment adds only 30 us
(so day 8's weakref design is not the problem). This is the same per-value cost day
8 flagged; scoping reduced how *often* it is paid, not the price. Lowering it is
day 40's job (a faster serialisation than `repr`, and a capturer that does less per
node).

---

## Day 10 — Phase 1 checkpoint (consolidated)

The numbers Phase 1 is judged on, in one place, all measured on the machine at the
top of this file.

| metric | value | day | note |
|---|---:|---:|---|
| Recording size cut, realistic | **−97.9%** | 8 | content-addressed dedup |
| Control-flow overhead cut, realistic (scope) | **33×** (180→5.4) | 9 | `DISABLE` filtering |
| Control-flow overhead, realistic | **5.4×** | 9 | under ADR-0001's 20× trigger |
| Control-flow overhead, tight loop | 102× | 9 | worst case; `pdb` is 50–100× |
| Dedup hit rate, realistic | 99.6% | 8 | |
| Event size (dataclass) | 151 B/event | 4 | `dataclass(slots=True, frozen=True)` |
| 1M events live in `MemorySink` | 225 MB, 216 B/event | 10 | see below |
| Per-value capture cost (large value) | 827 µs | 9 | day-40 target |

### 1M-event memory — Phase 2's justification, written down

A 260,000-iteration loop recorded control-flow-only produces **1,040,007 events**
and peaks at **225 MB** (216 B/event; the 65 B over the raw 151 B dataclass is the
`list`, the intern tables and live-object overhead). `tracemalloc` peak, not RSS:
stdlib, cross-platform, and it measures the Python allocation we control.

225 MB for a *modest* program held entirely in RAM is exactly why Phase 2 exists.
A ten-minute program would not fit; the file store (mmap + zstd, keyframes +
deltas) is not a nice-to-have but the thing that makes long recordings possible at
all. The ceiling test asserts < 400 MB so CI has headroom across platforms; the
real number is recorded here so day 14's compression has a baseline to beat.

### Divergence from the day-2/day-3 spike numbers

ADR-0001's table (spike, day 3) put realistic naive capture at 2,370× and change
detection at 6.1×. The shipped recorder is **not** 6.1× with capture on, and the
difference is honest: the 6.1× spike scoped to a single module *and* used an
unsound `id()` shortcut on mutable lists. The shipped design refuses that shortcut
([ADR-0003](../docs/adr/0003-dedup-correctness.md)) and re-captures every value,
so its realistic figure is capture-bound (day 40). What the spike got right is the
*control-flow* story: scoped flow-only is 5.4×, inside the range the spike
promised. The value-capture speed is the one number that moved against us, it is
measured (827 µs/value), and it has a dated owner (day 40) rather than a wish.

---

## Day 12 — write throughput (`bench_store_write.py`)

`ChronoWriter` turning a recorded event stream into `.chrono` bytes. Events come
from recording a real workload, so the column distributions are realistic. Sizes
are **pre-zstd** (compression is day 14); the columnar codecs here exist to make
zstd's job easy, not to be the compressor.

| workload | events | MB out | B/event | Mevents/s | MB/s |
|---|---:|---:|---:|---:|---:|
| tight_loop | 750,007 | 12.00 | 16.00 | 0.13 | 2.1 |
| json_pipeline | 195,776 | 4.13 | 21.12 | 0.12 | 2.6 |
| fib_recursive | 600,198 | 18.01 | 30.00 | 0.12 | 3.6 |

Real file + close-time fsync: 750k events, 12.0 MB, 0.13 Mevents/s, 2.2 MB/s --
i.e. fsync-at-close is free next to the encoding, which is the whole point of not
fsyncing per block.

**Two honest readings:**

* **16–30 B/event is pre-zstd, and expected.** Day 11 measured columnar **+ zlib**
  at 0.5–1.2 B/event; the raw columns are larger because the compressor has not run
  yet. The size is dominated by `timestamp_ns`: its per-line deltas are not constant
  (each line takes a slightly different time), so delta-rle cannot collapse them —
  but they are small numbers that zstd compresses well on day 14. seq, kind,
  thread_id, code_id and the `-1` columns already collapse to a handful of bytes via
  rle / delta-rle (a column stored raw is caught by `test_columnar`'s size assert).
* **~130k events/s is encoding-bound, and the writer is currently the slower half.**
  The recorder emits at ~273k events/s (day 5); the writer's rle/delta passes are
  Python loops and it tries all three codecs per column. That makes the writer the
  bottleneck today — a day-40 target (C-level codec passes, or a cheaper per-column
  selection), recorded rather than optimised speculatively. zstd (day 14) will
  change this profile, so tuning the codec selection before it lands would be
  premature.

---

## Day 13 — read path (`bench_store_read.py`)

`ChronoReader` over a 1M-event file. The file here is synthetic with a constant
timestamp stride, so its 1.1 B/event is *not* a compression figure (day 12's
`bench_store_write` has the real ones) -- this benchmark measures latency, not size.

| metric | value | meaning |
|---|---:|---|
| open (lazy) | **130 KiB** heap | the block index for ~15 blocks -- O(blocks), not O(events). Decoding all 1M events would be ~150 MB, so open touches ~0.1% of that. |
| sequential `__getitem__` | **3.0 µs** | LRU-warm; the timeline scrubber's dominant pattern (adjacent seqs land in the cached block). |
| random `__getitem__` | 11 µs median | a random jump either hits the LRU or decodes a whole block. |

**Lazy open is the headline.** Opening a 1M-event recording allocates 130 KiB of
Python heap -- the seq index, one entry per block -- and faults in *no* event pages
until something is read. This is the entire reason for mmap + lazy blocks: a 10 GB
recording opens in the same 130 KiB and costs RSS only for the pages the scrubber
actually visits.

**The random-access tail is a cold block decode, and it carries the recorder's GC
cost.** A random jump to an uncached block decodes ~65536 events at once (the
"decode the whole block" trade that makes sequential scrubbing 3 µs), which
allocates ~65536 `Event` objects and can trip a gen-2 GC pause -- the same
short-lived-object pressure flagged for day 40. Sequential access, the common case,
never pays it.

---

## Standing budgets

| thing | budget | current | measured |
|---|---:|---:|---|
| 1M events in `MemorySink` | < 400 MB (day 10) | **225 MB** | day 10 |
| Recording size, realistic workload | smaller is better | −97.9% | day 8 |
| Recorder overhead, realistic workload, **flow** | < 20x (ADR-0001 trigger) | **5.4x** | day 9 |
| Recorder overhead, realistic workload, +capture | < 20x eventually | ~2100x* | day 9 |
| Recorder overhead, tight loop, flow | — | 102x | day 9 |

\* The **flow** stream now clears the trigger: scoping (day 9) took realistic
flow-only overhead to 5.4x. With value capture on it is still ~2100x, and that
remaining cost is `capture()` itself (827 us per big value, day 9 table above),
not scope or event volume -- day 40's optimisation target, isolated and measured.
