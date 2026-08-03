# ChronoTrace

**A time-travel debugger for Python.** Record your program once, then scrub backward through its execution to find the bug.

<!-- HERO GIF (clip #8): the browser UI scrubbing BACKWARD from a wrong total to the aliased dict.
     When recorded, replace this comment with the image:
     <p align="center"><img src="docs/media/hero-scrub.gif" alt="Scrubbing backward through a recording to find an aliased dict" width="800"></p> -->

> ▶ **The demo, in one motion:** run a pipeline that prints three identical regional totals — no
> traceback, every `+=` looks right. Drag the timeline **backward** and watch `total` *un-change*
> until, 815 events before the symptom, the three regions are already the **same dict**. A bug
> found by travelling through time, not by re-running. *(Clip #8, recorded from the UI below.)*

> **Status:** the **engine is done** (`v0.4.0-query`) and the **browser tier (Phase 5) is landing** —
> a timeline scrubber, source + execution heatmap, variables that change *backward*, a full call
> tree, and a registry-driven query panel, all over a small local API. Recording, the durable
> `.chrono` store, reconstruction with backward stepping, and the causal query engine all work
> today. This README is honest about what runs and what is still being recorded for the demo.

## Why

Debugging only goes forward. Breakpoints show you the program *now*, so the
moment you realise the bug happened 200 steps ago, you restart and guess where
to break — over and over. The information you needed was computed once and then
thrown away. ChronoTrace keeps it.

## Finding a real bug, backwards

`examples/buggy_pipeline.py` prints three regional totals that are all identical —
obviously wrong, no traceback, and every `+=` looks correct if you step forward.
The cause is 815 events upstream: `dict.fromkeys` evaluates its default **once**, so
all three regions share one dict.

```bash
chronotrace step examples/buggy_pipeline.py
```

```
 north: $11235.00 (90 orders)      ← the symptom: three identical totals
 south: $11235.00 (90 orders)
  east: $11235.00 (90 orders)
882 events. `?` for help, `q` to quit.

(chrono) g 869                     # the instant the report was built
[869] main (buggy_pipeline.py):60
(chrono) p report
report = {"north": {"sales": 11235.0, "orders": 90},
          "south": {"$": "cycle"},   ← not a copy. the same object.
          "east":  {"$": "cycle"}}
(chrono) F                         # back to where this frame was called
[48] main (buggy_pipeline.py):58
(chrono) g 54                      # ...and back to the very first write
[54] build_report (buggy_pipeline.py):51
(chrono) p totals
totals = {"north": {"sales": 0.0, "orders": 0},
          "south": {"$": "cycle"},   ← already aliased, before a single order
          "east":  {"$": "cycle"}}
```

The bug is visible at **seq 54** with all-zero totals — 815 events before the symptom
it causes. That is the whole product thesis: the evidence was computed once and thrown
away, and this keeps it.

## Every command, in both directions

```bash
chronotrace step examples/simple.py
```

```
(chrono) g 39                    # jump to an instant
[39] double (simple.py):19
(chrono) bt
* #5 double (simple.py):19
  #3 quadruple (simple.py):24
  #2 main (simple.py):31
  #1 <module> (simple.py):36
(chrono) p n                     # what was n, at that instant?
n = 0
(chrono) p                       # previous line
[37] double (simple.py):18
(chrono) p                       # back out of the call that just ran
[34] quadruple (simple.py):24
(chrono) O                       # step over, backward -- skips the whole call
[26] quadruple (simple.py):23
```

| forward | backward | what it does |
|---|---|---|
| `n` | `p` | the next/previous line, in any frame — "step into", both ways |
| `o` | `O` | the next/previous line **in this frame** — nested calls are skipped whole |
| `f` | `F` | run to where this frame exits / back to where it was called |

Backward commands are the *same code* as their forward twins with the sign of the
scan flipped, so they cannot disagree: `step_back(step_forward(seq)) == seq` is
asserted at every stop instant of every example recording.

| reaching an instant in a 281k-event recording | measured |
|---|---:|
| cold random jump | **12 ms** p50 |
| one step through the locality cache | **65 µs** p50 |
| one step backward | **715 µs** p50, 1.5 ms p99 |
| replay depth vs. the ≤ 1,000 contract | **996** — holds |

**[How it works →](docs/how-it-works.md)** — keyframes and deltas, why frames are a
registry rather than a stack, and how correctness is proven against an
independently-observed ground truth.

Asking for a variable the program had not reached yet says so, rather than showing
you `None`:

```
(chrono) p result
result is not bound in this frame at seq 26
```

## Retroactive breakpoints

Set a breakpoint **after the program has already finished**, and instantly see every time
it would have hit. No re-running — there is nothing left to run. This is the thing `pdb`
structurally cannot do, and the end of the "I put the breakpoint in the wrong place, run it
again" loop.

```bash
chronotrace query run.chrono --break pipeline.py:42            # every hit of line 42
chronotrace query run.chrono --break pipeline.py:42 --if "i > 100"   # only where i > 100
```

The condition is **never `eval`'d.** It is user-supplied source, so it is parsed and walked
by a restricted evaluator over a whitelisted grammar — no calls, imports, lambdas, or dunder
access, all rejected at parse. And it evaluates over *captured data*, not live objects, so
there is nothing dangerous for it to reach in the first place. Two properties it will not
give up:

- **It matches a live debugger exactly.** A test runs the program under a real `pdb`
  conditional breakpoint and asserts ChronoTrace's retroactive hits are the identical set —
  the breakpoint you set afterwards finds precisely what the breakpoint you set beforehand
  would have.
- **It never lies about what it could not see.** A hit where the condition needed a value the
  recording only summarised (a truncated list, a redacted secret) is returned flagged
  *unknown*, never silently answered `false`.

Watchpoints come free from the same index — `--watch total` shows every change as
`old -> new` — and reverse-continue to a breakpoint is an indexed lookup, measured **217×**
faster than the old linear scan. **[Full reference →](docs/queries.md)**

## ChronoTrace vs `pdb`

Three bugs it had never seen — an off-by-one, a mutable default argument, a late-binding
closure — were each debugged with **one or two queries**, no prints and no re-running
([the session, step by step](docs/tutorial.md)). But a tool author who knows exactly where
their tool loses is more credible than one who claims it always wins:

| | `pdb` | ChronoTrace |
|---|---|---|
| **move a breakpoint after the fact** | re-run the program | one query, nothing re-runs |
| **the whole history of a variable** | step and watch, iteration by iteration | `--watch x` — every change at once |
| **cause far in the past from the symptom** | guess where to break, repeat | `--provenance`, `--exception-origin` jump straight there |
| **a flaky bug** | may not reproduce this run | recorded once, replays deterministically |
| **attach to a running / production process** | ✅ its home turf | ✗ needs a recording first |
| **zero setup, zero overhead** | ✅ | ✗ record step + a few % while recording |
| **interactive poking around** | ✅ a live REPL in the frame | partly — `chronotrace step`, but not arbitrary eval |
| **threads racing, C-extension internals, memory bugs** | ✅ sees the live machine | ✗ only what the events recorded |

The rule of thumb: ChronoTrace wins on **state and causal questions**, especially when the
mistake and the symptom are far apart in time — the loop of "re-run and break earlier" is
the thing it removes. Reach for `pdb` when you need to be *inside a live process*, or the bug
lives somewhere the recording does not reach.

## What works today

```bash
pip install -e .
chronotrace record examples/buggy_pipeline.py
```

This runs the target under the recorder and reports the event count. The recorder
(built on PEP 669 `sys.monitoring`) captures:

- **Control flow** — every line, call, return, and the full exception lifecycle
  (raise origin, unwind, handled), with generators and `async`/`await` recorded
  correctly (a suspended frame keeps one identity across its whole life).
- **Local values** — captured without ever invoking the program's own code
  (no `__repr__`, no property, no `__getattr__` side effects), without keeping any
  recorded object alive, and safely across cycles, 10-million-element lists and
  hostile objects.
- **Only your code** — the standard library and site-packages are excluded by
  default (`--include` to debug into a dependency).
- **No secrets** — locals named like `*password*`, `*token*`, `*secret*` are
  withheld *before* they are read, never scrubbed after.

`chronotrace step script.py` records into the real `.chrono` format and opens the
stepping session on it, so the demo above exercises the whole pipeline — writer,
reader, reconstruction — not a shortcut. `chronotrace step rec.chrono` opens a saved
recording, but renders numeric ids instead of names: the format does not yet persist
its intern tables ([#6](https://github.com/dharmppp21/ChronoTrace/issues/6)).

## Browse a recording over HTTP

The browser UI (Phase 5) talks to a small local API. It ships behind an optional
extra, so recording stays dependency-light — install it only if you want to serve:

```bash
pip install -e ".[ui]"
chronotrace serve --dir .          # serves every .chrono in the directory
chronotrace record --ui app.py     # record live: watch the timeline fill, then scrub it
```

`--ui` streams the recording to disk as the program runs and serves it live over a WebSocket,
so the timeline fills in real time and becomes fully scrubbable the instant the program ends.
It is a live *view*, not a live debugger — no pausing the target, no interactive breakpoints
(those are post-1.0). The visual scrubber UI lands in Phase 5; until then `--ui` opens the API
explorer.

Then time-travel with `curl` (interactive docs at `http://localhost:8000/docs`):

```bash
curl "localhost:8000/api/sessions"                      # list recordings
curl "localhost:8000/api/sessions/<id>/state?seq=400"   # reconstructed state at an instant
```

The server binds `127.0.0.1` only, validates the `Host` header, and locks CORS to the
UI origin — a recording is your program's memory, and localhost is not a security
boundary against a malicious web page. See [`docs/api.md`](docs/api.md) and
[ADR-0010](docs/adr/0010-api-contract.md) for the contract, caching model, and threat model.

### The five panels

The whole UI is a **pure function of one event index** — move the playhead and every panel
re-renders the same instant together. It is a *scrubber, not an IDE*: read-only, one recording,
localhost (ADR-0011). Full guide, with keyboard shortcuts, in [`docs/ui.md`](docs/ui.md).

<!-- PANEL SCREENSHOTS: add each as docs/media/panel-*.png and uncomment.
     <p align="center"><img src="docs/media/panel-timeline.png" alt="Timeline scrubber" width="800"></p> -->

- **Timeline** — the recording as a density band; drag to travel (rAF-coalesced, cancellable).
- **Source + heatmap** — the executing file, log-scaled per-line execution counts; click the gutter for a **retroactive breakpoint**.
- **Variables** — locals as previews, lazily expandable; scrub backward and watch them change backward, with `old → new` diffs from the invertible deltas.
- **Call tree** — not just the current stack but *every call the program ever made*, coloured by how each ended (returned / **raised** / open); click one that returned 200k events ago and go there.
- **Query** — forms generated from the engine's query registry; results are hoverable, jump-to-instant links.

## Overhead, measured against the tools you already use

All numbers ÷ a no-instrumentation baseline, medians of 5, one fresh subprocess per
sample. i5-13450HX, Windows 11, Python 3.14. Regenerate with `python -m benchmarks`;
full matrix, p95s and the environment header are in
[`benchmarks/RESULTS.md`](benchmarks/RESULTS.md), the rules in
[`benchmarks/METHODOLOGY.md`](benchmarks/METHODOLOGY.md).

| Workload | `pdb`¹ | coverage.py² | **ChronoTrace flow**³ | ChronoTrace +capture⁴ |
|---|---:|---:|---:|---:|
| Realistic pipeline (stdlib-heavy, scoped) | ~35× | ~1.5× | **~6×** | ~1,500× |
| Recursive / call-heavy | ~170× | ~1.2× | ~310× | ~550× |
| Tight numeric loop (worst case) | ~115× | ~1.2× | ~200× | ~2,500× |
| I/O-bound (the control) | 1.0× | 1.0× | 1.0× | 1.0× |

Rounded, because the worst-case (tight-loop) cells allocate 750k+ objects and vary
±~20% run to run; [`RESULTS.md`](benchmarks/RESULTS.md) carries the exact last-run
medians and p95s with the environment header.

**Each column does strictly more work than the one to its left — so this is not a
like-for-like race, and the table says so rather than hiding it:**

1. **`pdb`** — a never-hit breakpoint left attached: full per-line dispatch, what people actually pay.
2. **coverage.py** — records only *which lines ran* (a set), and on 3.14 `DISABLE`s each line after its **first** hit, so a hot loop is nearly free. It does far less than a trace; its cheapness is *because* it records less, not because it is faster per event.
3. **ChronoTrace flow** — the default: the full **ordered** event stream (every LINE/CALL/RETURN), scoped to your own code. On realistic code it is **~6×, roughly 6× cheaper than `pdb`** — because scope filtering (`DISABLE` on the stdlib) cut this workload from ~197k recorded events to 13k. Recording an ordered stream is why it costs more than coverage's set.
4. **ChronoTrace +capture** — every variable's *value* at every line. This is the opt-in deep-inspection mode and it is what costs the four-figure numbers; reach for it when you need to *see* state, not just flow.

The worst case (tight loop + capture, **~2,500×**) is the number a hostile reader
should quote, so it is in the table, not a footnote. It is high because sound
change-detection must re-walk mutable locals every line; the levers are narrower
`--include` scope and `--sample` (record every Nth hit — **planned, issue #18**, not
yet built, so it has no measured row). A native (Rust) capturer was profiled and
**deliberately declined** — Amdahl caps the win at ~3.6× for a week of work plus a
permanent build matrix ([ADR-0014](docs/adr/0014-no-native-extension.md)). Phase 6
also corrected these numbers: earlier tight-loop figures (~102×/1,270×) were stale —
the recorder evolved through day 42 and they no longer reproduce under the current
subprocess-isolated, GC-honest suite, which supersedes them.

## When *not* to use ChronoTrace

An honest tool names its own failure modes. Do not reach for ChronoTrace when:

- **The bug is timing-dependent — a race, a deadlock window, an ordering hazard.**
  Recording changes timing: every callback and value capture adds latency the real
  program never pays, so the bug **may not reproduce under recording, or may
  reproduce differently.** This *observer effect* is a property of observing a
  running program (every tracer here, `pdb` included, perturbs timing), not a defect
  we can fix. For these, reach for a live debugger or targeted logging.
- **The hot path is a tight numeric loop and you need value capture.** Overhead
  tracks Python lines executed, so a million-iteration inner loop with capture on is
  the ~2,500× case above. Record it flow-only, narrow the `--include` scope, or wait
  for `--sample`.
- **It's production.** ChronoTrace is a *development* debugger. The overhead is
  opt-in and the recordings are large; this is not an always-on production tracer.
- **Recordings must stay small.** A value-capture recording is bytes-per-event tiny
  after dedup + zstd, but a long run is still large on disk — a debugger trades disk
  for the ability to step backwards, and that trade is not always the one you want.

For I/O-bound programs, code you can scope tightly, and bugs you'd otherwise chase by
re-running under `pdb`, it is a good trade — which is the rest of this README.

## How it works

State is stored the way a video codec stores frames: **full keyframes every N
events, deltas in between.** Reaching any past instant is a binary search to the
nearest keyframe plus a bounded number of deltas — which is what makes scrubbing
feel instant instead of requiring a re-run. Deltas store the **old** reference as
well as the new, so they can be undone, which is what makes backward stepping
cheap. [The long version.](docs/how-it-works.md)

```
 target.py ─▶ recorder ─▶ store ─▶ index ─▶ reconstruct ─▶ query ─▶ server ─▶ UI
            (sys.monitoring) (mmap+zstd) (sqlite)  (keyframe+deltas)
```

Dependencies point one way only: `server → query → reconstruct → index → store →
recorder`, enforced by an import-graph test. See
[`docs/architecture.md`](docs/architecture.md) and the [ADR log](docs/adr/).

**Recordings survive crashes.** A debugger records programs that crash — so a recording
must be readable when the process is killed mid-write, not only after a clean exit. Each
block is framed with a length and a CRC and flushed to the OS as it completes, so a
recovery scan returns the intact prefix and discards the torn tail whole (never a
half-decoded, invented event). The proof is a test that spawns real recording processes,
kills them (`SIGKILL`/`TerminateProcess`) at random instants, and asserts every file
still opens — `tests/store/test_crash_real.py` (set `CHRONOTRACE_KILL_ITERS=100` for the
full run). `chronotrace repair rec.chrono` rebuilds a footer for a crashed recording
without ever modifying the original in place.

**The `.chrono` format, measured.** A versioned, CRC-framed, zstd-compressed columnar
log with keyframe+delta state encoding — the full byte layout is
[`docs/format-spec.md`](docs/format-spec.md), and every default was chosen by grid search
against a stated objective ([ADR-0005](docs/adr/0005-storage-defaults.md)), not taste:

| Metric | Value | |
|---|---:|---|
| On-disk size | **~5 bytes/event** | vs 151 B/event live in RAM |
| Random access to any `seq` | **~9 ms cold**, ~1 µs cached | decodes one 4096-event block |
| State reconstruction at any instant | **~2.7 ms** | nearest keyframe + ≤ 1000 deltas |
| Backward step | **1 delta inverted** | O(1), never a rewind to a keyframe |

The block-size choice is a 15× random-access speedup over the naive compression optimum
— the curve is [`benchmarks/plots/block_size.svg`](benchmarks/plots/block_size.svg), and
the interval tradeoff is
[`keyframe_interval.svg`](benchmarks/plots/keyframe_interval.svg).

| Capability | Phase | Status |
|---|---|---|
| Recording (lines, calls, values, exceptions) | 1 | **done** |
| `.chrono` format (framed, zstd, columnar, keyframe+delta, crash-recoverable) | 2 | **done** |
| Backward stepping / scrubbing | 3 | **done** |
| Causal queries ("who last wrote to `total`?") | 4 | **done** (`v0.4.0-query`) |
| Browser timeline UI (five panels over a local API) | 5 | **shipping** — engine + API done; UI landing |

## Requirements

- Python 3.12+ (3.14 recommended). On 3.12, one edge case leaks a frame for a
  garbage-collected generator — a CPython limitation fixed in 3.13, documented in
  [ADR-0002](docs/adr/0002-frame-registry.md).
- Linux, macOS or Windows. Zero runtime dependencies (the recorder is imported
  into your program; it must not drag its own dependency tree in).

## Development

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the standards this project holds itself
to, and [docs/adr/](docs/adr/) for why it is built the way it is.

## A note on recordings

A recording contains the full memory of the program that produced it — including
any credentials, tokens or personal data that program held. **Treat a recording
as you would a core dump.** Secret-named locals are redacted at capture time, but
that is a safety net, not a guarantee (a secret in a variable named `x` is not
caught). A threat model lands in Phase 7.

## License

MIT — see [LICENSE](LICENSE).
