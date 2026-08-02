# Recorder & pipeline profile (day 40)

**The discipline of the day: measure, do not optimise.** This file is the deliverable — a
prediction made *before* profiling, the reality, the gap, the top bottlenecks with triage, and
the Rust-extension verdict computed from Amdahl's law on real numbers. Zero optimisations shipped.

Machine: i5-13450HX, Windows 11, Python 3.14. Tools: `py-spy` 0.4.2 (sampling, primary),
`cProfile` (call counts), `time.perf_counter_ns` micro-timers. Harness: `profile_recorder.py`.

---

## 1. Prediction (written before any measurement)

What I expect the recorder's time to go on, with **value capture ON**, ranked by share of the
recorder's overhead. Recorded honestly so the gap against reality is visible.

1. **Value capture traversal** (`capture()` walking the object into bounded data) — **dominant,
   ~55–70%.** The recorder's own hot-path comment already says "against `capture()`, which
   dominates it", and day-9 measured `capture()` at 827µs for a 512-node value.
2. **Content digest** (`dedup.digest` = `repr(captured)` + blake2b) — **~15–25%.** `repr` walks
   the just-built tree a *second* time; day-9 put it at 207µs of a 249µs digest.
3. **Monitoring dispatch + scope check** (CPython → Python `_on_line` per line) — **~5–10%.**
4. **Dedup pool lookup + `Event` construction + `perf_counter_ns`/thread-id** — **~5–10%.**
5. **Store side** (msgpack, columnar, zstd, file writes) — **negligible here.** A `MemorySink`
   run does not exercise it, and even with `FileSink` it is per-*block* amortised, not per-line.

**Overall thesis:** capture + digest ≈ **80–90%** of recorder overhead when capture is on.
Corroborating prior: RESULTS.md shows ~2100× overhead with capture vs ~5.4× flow-only, so capture
is ~99% of the *marginal* cost of turning capture on. I expect the flamegraph to be one giant
`capture` tower with a `digest` sibling, and almost nothing else visible.

**Predicted Rust verdict:** capture + digest are pure-Python per-node work a native extension
*could* replace. If they are ~85% of recorder time, Amdahl caps a Rust capturer's win at a real
multiple — so Rust is *technically justified for the value capturer specifically*. But flow-only
recording already clears ADR-0001's 20× budget without it, so I predict the honest verdict is
**"justified but deferred": build the Rust capturer only when capture latency demonstrably blocks
a real user, not now.**

**Where I expect to be wrong (the interesting part):** (a) `digest` might rival or *beat* capture,
because `repr` is a second full walk in C but over Python containers; (b) GC churn from per-value
dict allocation might be a bigger hidden cost than the time profile shows; (c) the per-line
Python-level overhead of `_on_line` itself (the loop over locals, dict lookups) might be larger
than 10%.

---

## 2. Reality (py-spy, sampling, undistorted)

Self-time shares from py-spy raw collapsed stacks. **The realistic workload (`json_pipeline`,
2611 samples) is the one to reason from** — `tight_loop` is the hostile-reader's worst case, not a
typical program (its docstring says so). Both are shown because they differ *wildly*, which is
itself the finding.

**`json_pipeline` — captures real dicts/lists (the typical shape):**

| bottleneck | self-time | what it is |
|---|---:|---|
| **value capture traversal** | **~55%** | `_mapping` 14% + `_capture` 12% + `_tagged` 9.5% + `_string` 6.7% + `_handler_for` 4.8% + `_sequence`/`_cycle_or_depth`/`_capture_locals`/`_atom` |
| **content digest** (`repr` + blake2b) | **25%** | the single hottest *leaf* |
| **`ObjectIdentity.of`** (aliasing ids) | **7%** (13% incl.) | weakref bookkeeping per captured object |
| **allocation** (`__new__`) | **6%** | the captured-value dicts |
| redaction + scope (`fnmatchcase`) | ~2% | cheap here — the values dominate |

**`tight_loop` — captures only scalars (worst case, but atypical):**

| bottleneck | self-time |
|---|---:|
| **redaction name-check** (`should_redact` -> `fnmatchcase`, per local per line) | **~30%** |
| digest | 13% |
| capture traversal (`_capture_locals` + `capture` + genexpr) | ~20% |
| event emit (`_emit`, `__init__`) | ~12% |

**Micro-timers — which half of a single capture, by value shape:**

| value shape | capture ns | digest ns | digest share |
|---|---:|---:|---:|
| `int` | 453 | 622 | **58%** |
| 4-key dict | 2 998 | 1 819 | 38% |
| 200-record list | 159 091 | 68 410 | 30% |

The digest's share *falls* as the value grows: `blake2b` + `repr` is a ~600 ns **fixed tax per
capture**, so it dominates cheap values and is amortised by expensive ones. `capture` only wins
outright once the value is large.

**Pathology checks:** GC is healthy -- **gen-2 collections = 0** in both runs (the day-8
gen-2-pause worry did not reproduce), gen-1 ~27, memory linear (276 B/event tight, 3099 B/event
json, no leak). `DISABLE` still fires (`test_scope_filter` green) -- no 30-day regression.
`cProfile` is **blind** to the `sys.monitoring` callbacks (they bypass `sys.setprofile`), so it
reports a useless flat profile here -- a concrete reason py-spy is primary, not a nicety.

## 3. Prediction vs reality -- the gap (the honest part)

| I predicted | reality | verdict |
|---|---|---|
| capture traversal #1, ~55-70% | capture *subtree* ~55%, but spread thin; no single dominant leaf | half right |
| digest #2, 15-25% | digest is the **single hottest leaf (25%)**, and *dominates* cheap values (58%) | **underweighted** |
| scope/redact negligible | **redaction `fnmatch` is ~30% of tight_loop** | **badly wrong** |
| identity minor ("30 us") | `ObjectIdentity.of` is **7-13%** | **underweighted** |
| store/scope-DISABLE ~0 | confirmed ~0; DISABLE works | right |

**Three misses worth owning:** (1) I treated the digest as a small tail; it is the hottest single
function and a fixed per-value tax. (2) I dismissed redaction entirely -- it is the *dominant* cost
when values are cheap, because `should_redact` runs `fnmatch` against ~6 secret-name globs for every
local on every line. (3) `_handler_for`'s isinstance dispatch (~5%) contradicts day-7's own finding
that an exact-type dict (113 ns) beats an isinstance chain (202 ns) -- a possible 30-day regression.

## 4. Top 5 bottlenecks -- fraction, fix, cost, risk (all deferred to Days 41+)

1. **Value capture traversal (~55% json).** Fix: fuse capture-and-hash into one pass; faster type
   dispatch. Cost: medium (Python) / high (Rust). Risk: **high** -- capture correctness (hostile
   inputs, cycles, the node budget) is load-bearing; the referee guards it but this is the scariest
   code to touch.
2. **Content digest -- `repr` + blake2b (~25% json, 58% of a scalar).** Fix: hash the captured tree
   *structurally* during capture, skipping the intermediate `repr` string and its allocation. Cost:
   medium. Risk: medium -- dedup's "equal content -> equal bytes" invariant must survive.
3. **Redaction `fnmatch` (~30% tight_loop, ~2% json).** Fix: compile the secret-name globs to **one**
   regex, or cache the redact decision per `(code, line)` -- the local names on a line are static.
   Cost: **low (a few lines)**. Risk: **low**. **This is the best ROI on the board** and needs no
   Rust: a ~30% win on cheap-value recordings for an afternoon's work.
4. **`ObjectIdentity.of` (~7-13%).** Fix: make identity assignment lazy -- it exists only for the UI
   aliasing badge (issue #9), so skip it unless a recording is opened in the UI. Cost: low-medium.
   Risk: low (feature-gated).
5. **`_handler_for` isinstance dispatch (~5%).** Fix: an exact-type dict lookup before the isinstance
   fallback (day-7's own measured design). Cost: low. Risk: low.

## 5. Cold paths -- deliberately NOT profiled deeper, and why

The recorder runs **per line of the user's program** -- a 1 us saving is worth millions of times
over. The rest of the pipeline runs **when a human drags something**:

| stage | when | measured (RESULTS.md) | verdict |
|---|---|---|---|
| index build | once per recording | ~156k events/s, block-decode-bound (day 27) | leave it |
| reconstruction (`/state`) | per playhead drag | 155 us p95 warm, ~45 ms cold (day 39) | leave it |
| query | per human query | < 50 ms at 10M (day 28) | leave it |
| WS streaming | per 100 ms tick | one bounded frame/tick (day 34) | leave it |

A 1 ms reconstruction win is invisible -- it happens once, behind a human reflex. **Optimising a
cold path is the classic wasted week.** Today's data is exactly how we avoid spending it: every
lever worth pulling is in the recorder's per-line hot path, and none is downstream.

## 6. The Rust verdict -- Amdahl's law on real numbers

**Question:** what fraction of the recorder's overhead is in Python a native extension could plausibly
replace? A Rust capturer would fuse traversal + structural hash + identity + allocation into one
native pass. That is capture (55%) + digest (25%) + identity (7%) + allocation (6%) ~= **~88%** of
`json_pipeline` overhead. The unreplaceable ~12% is CPython's `sys.monitoring` dispatch and the
Python glue in `_on_line` -- it is a Python callback by definition, and Rust cannot delete the call.

**Amdahl:** even an *infinitely* fast Rust core caps the recorder speedup at `1 / (1 - 0.88)` =
**~8x**. A realistic 6x-on-the-88%-part gives `1 / (0.12 + 0.88/6)` = **~3.7x**.

**Verdict: DEFER the Rust extension. This is a win, not a failure (the roadmap said so).** Three
data-backed reasons:

1. **The slow path is *only* value capture.** Flow-only recording is already 5.4x (RESULTS.md,
   under ADR-0001's 20x budget). Nothing forces the value-capture path faster *today* -- it is a
   quality-of-life win for value-heavy recordings, not a correctness or budget blocker.
2. **The cheap Python wins are un-banked.** Redaction-regex (~30% of tight_loop), structural digest
   (~25%), lazy identity (~10%), dict dispatch (~5%) are afternoons of Python work, together a large
   share of the ~3.7x a Rust rewrite would buy -- at a fraction of the cost and risk. Measure again
   *after* those before deciding Rust is needed.
3. **Rust breaks a load-bearing invariant.** The recorder is imported **into the user's process**
   and ships **zero runtime dependencies** (ADR-0001, `test_architecture`). A native extension is a
   compiled dependency in the traced program's address space, plus wheels for 3 OSes, a build matrix,
   and a debugging story -- roughly a week, and a permanent maintenance tax.

**The plan: exhaust the Python optimisations (Days 41+), re-profile, and reach for Rust only if
value-capture latency then demonstrably blocks a real user.** ADR-0013 records this decision.
