# ChronoTrace

**A time-travel debugger for Python.** Record your program once, then scrub backward through its execution to find the bug.

> **Status: Phase 1 complete — the recorder works; storage and UI do not yet.**
> ChronoTrace can record a Python program's full execution (every line, call,
> return, exception and local value) into an in-memory event stream, with value
> deduplication, scope filtering and secret redaction. It cannot yet **save** a
> recording to disk, scrub through it, or show a UI — those are Phases 2–5. This
> README describes what runs today and is honest about what does not.

## Why

Debugging only goes forward. Breakpoints show you the program *now*, so the
moment you realise the bug happened 200 steps ago, you restart and guess where
to break — over and over. The information you needed was computed once and then
thrown away. ChronoTrace keeps it.

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

The `record` command reports to an in-memory sink today; **Phase 2 (below) built the
durable, crash-recoverable `.chrono` store** the recorder writes into, and wiring it
behind `record` is the next step.

## Overhead, measured not boasted

i5-13450HX, Windows 11, Python 3.14, medians of 5. Full tables and methodology in
[`benchmarks/RESULTS.md`](benchmarks/RESULTS.md).

| Workload | Control-flow only | With value capture |
|---|---:|---:|
| Realistic pipeline (stdlib-heavy) | **5.4×** | capture-bound* |
| Tight numeric loop (worst case) | ~102× | ~1,270× |
| I/O-bound (the control) | 1.0× | 1.0× |

Content-addressed deduplication cuts recording size by **97.9%** on the realistic
workload; scope filtering via `DISABLE` cuts realistic control-flow overhead
**33×** (180× → 5.4×). For honest comparison, `pdb` is widely cited at 50–100×.

\* Value capture is correct and bounded but not yet fast — it re-serialises each
changed value; the per-value cost (~827 µs for a large value) is Phase 6's
optimisation target (day 40), measured and isolated, not hand-waved.

## How it will work

State is stored the way a video codec stores frames: **full keyframes every N
events, deltas in between.** Reaching any past instant is then a binary search to
the nearest keyframe plus a bounded number of deltas — which is what makes
scrubbing feel instant instead of requiring a re-run.

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
| Backward stepping / scrubbing | 3 | planned |
| Causal queries ("who last wrote to `total`?") | 4 | planned |
| Timeline UI | 5 | planned |

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
