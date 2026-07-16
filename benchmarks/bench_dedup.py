"""Measure what dedup + change detection actually buy, on the four Day-2 workloads.

Run: `python benchmarks/bench_dedup.py`

Two things are reported, because dedup has two effects and conflating them hides
which one is working:

* **Hit rate** -- of every (local, line) capture, the fraction whose content was
  already in the pool. This is the storage win: a high hit rate means the pool
  stores each distinct value once however many times it is read.
* **Recording-size reduction** -- naive day-7 recording (one VAR_WRITE event and
  one stored value per local per line) against today's (a VAR_WRITE only on
  change, each distinct value stored once). This is the headline number.

Sizes use the shipped event size (151 B/event, day 4) and the actual serialised
length of each captured value, so the percentage is grounded, not assumed.

The counting pool below wraps the real one rather than adding counters to
production code: the recorder must carry no benchmark scaffolding into the user's
hot path.
"""

from __future__ import annotations

import gc
import statistics
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

from workloads import WORKLOADS  # type: ignore[import-not-found]  # noqa: E402

from chronotrace.recorder import EventKind, MemorySink, Recorder  # noqa: E402
from chronotrace.recorder.capture import CapturedValue  # noqa: E402
from chronotrace.recorder.values import ValuePool, ValueRef  # noqa: E402

_EVENT_BYTES = 151  # day 4, benchmarks/RESULTS.md
_ORDER = ("tight_loop", "fib_recursive", "json_pipeline", "io_bound")
_REPEATS = 5


class CountingPool(ValuePool):
    """A ValuePool that also records how often dedup fired and the bytes involved."""

    __slots__ = ("adds", "distinct_bytes", "hits", "naive_bytes")

    def __init__(self) -> None:
        super().__init__()
        self.adds = 0
        self.hits = 0
        self.naive_bytes = 0  # what day 7 would have stored: every add
        self.distinct_bytes = 0  # what we store: first sighting only

    def add(self, captured: CapturedValue) -> ValueRef:
        self.adds += 1
        size = len(repr(captured))
        self.naive_bytes += size
        before = len(self._values)
        ref = super().add(captured)
        if len(self._values) == before:
            self.hits += 1
        else:
            self.distinct_bytes += size
        return ref


def _account(name: str) -> dict[str, Any]:
    """One deterministic run, counting dedup and both recording sizes."""
    fn = WORKLOADS[name]
    rec = Recorder(MemorySink(), capture_values=True)
    pool = CountingPool()
    rec._values = pool  # swap in the counting pool before instrumentation starts
    with rec:
        fn()

    events: list[Any] = rec.sink.events  # type: ignore[attr-defined]
    emitted = sum(1 for e in events if e.kind is EventKind.VAR_WRITE)

    naive = pool.adds * _EVENT_BYTES + pool.naive_bytes
    now = emitted * _EVENT_BYTES + pool.distinct_bytes
    return {
        "adds": pool.adds,
        "emitted": emitted,
        "hit_rate": pool.hits / pool.adds if pool.adds else 0.0,
        "distinct": len(pool._values),
        "reduction": 1 - now / naive if naive else 0.0,
    }


def _overhead(name: str) -> tuple[float, float]:
    """Recorder overhead as a multiple of baseline: (median, p95 by nearest-rank)."""
    fn = WORKLOADS[name]
    fn()  # warm import-time caches

    base = []
    for _ in range(_REPEATS):
        gc.collect()
        t0 = time.perf_counter()
        fn()
        base.append(time.perf_counter() - t0)
    baseline = statistics.median(base)

    ratios = []
    for _ in range(_REPEATS):
        gc.collect()
        rec = Recorder(MemorySink(), capture_values=True)
        t0 = time.perf_counter()
        with rec:
            fn()
        ratios.append((time.perf_counter() - t0) / baseline)
    ratios.sort()
    return statistics.median(ratios), ratios[-1]  # n=5: nearest-rank p95 is the max


def main() -> int:
    print(
        f"{'workload':<15} {'captures':>10} {'emitted':>9} {'hit%':>8} {'distinct':>9} {'cut%':>8}"
    )
    print("-" * 61)
    for name in _ORDER:
        a = _account(name)
        print(
            f"{name:<15} {a['adds']:>10,} {a['emitted']:>9,} "
            f"{a['hit_rate']:>8.1%} {a['distinct']:>9,} {a['reduction']:>8.1%}"
        )

    print(f"\n{'workload':<15} {'overhead x (median)':>20} {'p95':>9}")
    print("-" * 46)
    for name in _ORDER:
        median, p95 = _overhead(name)
        print(f"{name:<15} {median:>19.1f}x {p95:>8.1f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
