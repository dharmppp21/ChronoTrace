"""Measure the DISABLE scoping win (day 9), isolated from the capture cost.

Run: `python benchmarks/bench_scope.py`

Why flow-only
-------------
Scoping's job is to stop recording code that is not yours -- the stdlib, your
dependencies. Its win is fewer *events*, which is cleanest to see with value
capture turned off, so the number is not swamped by capture cost (which day 8
measured as the dominant term and day 40 will optimise). Each workload is run
under two scopes:

* **narrow** -- the shipped default: record only the project tree, DISABLE the
  stdlib and site-packages.
* **wide** -- record everything except ChronoTrace itself (day 8's behaviour).

The gap between them is exactly what returning `sys.monitoring.DISABLE` for
out-of-scope code buys. Pure-Python-loop workloads (`tight_loop`, `fib`) have
nothing out of scope, so they are the control: scoping cannot and should not help
them. `json_pipeline`, which calls real stdlib (`strptime`, `statistics`), is
where the win shows up -- and it is the shape of code people actually debug.
"""

from __future__ import annotations

import gc
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

from workloads import WORKLOADS  # type: ignore[import-not-found]  # noqa: E402

from chronotrace.recorder import MemorySink, Recorder  # noqa: E402
from chronotrace.recorder.scope import Scope  # noqa: E402

_ORDER = ("tight_loop", "fib_recursive", "json_pipeline", "io_bound")
_REPEATS = 5
_WIDE = Scope(include=["*"])  # allow everything except ChronoTrace itself


def _baseline(fn: object) -> float:
    runs = []
    for _ in range(_REPEATS):
        gc.collect()
        t0 = time.perf_counter()
        fn()  # type: ignore[operator]
        runs.append(time.perf_counter() - t0)
    return statistics.median(runs)


def _run(fn: object, scope: Scope | None) -> tuple[float, int]:
    runs, events = [], 0
    for _ in range(_REPEATS):
        gc.collect()
        sink = MemorySink()
        rec = Recorder(sink, scope=scope, capture_values=False)
        t0 = time.perf_counter()
        with rec:
            fn()  # type: ignore[operator]
        runs.append(time.perf_counter() - t0)
        events = len(sink.events)
    return statistics.median(runs), events


def main() -> int:
    header = (
        f"{'workload':<15} {'wide x':>8} {'wide ev':>10} "
        f"{'narrow x':>9} {'narrow ev':>10} {'ev cut':>8}"
    )
    print(header)
    print("-" * len(header))
    for name in _ORDER:
        fn = WORKLOADS[name]
        fn()
        base = _baseline(fn)
        wide_t, wide_ev = _run(fn, _WIDE)
        narrow_t, narrow_ev = _run(fn, None)  # None -> the shipped default (narrow)
        cut = 1 - narrow_ev / wide_ev if wide_ev else 0.0
        print(
            f"{name:<15} {wide_t / base:>7.1f}x {wide_ev:>10,} "
            f"{narrow_t / base:>8.1f}x {narrow_ev:>10,} {cut:>7.1%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
