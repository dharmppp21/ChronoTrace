"""Day 20: reconstruction latency against the bounded-cost contract.

Run: `python benchmarks/bench_reconstruct.py`

The contract (ADR-0006): reaching any `seq` is one `O(log K)` keyframe lookup plus a
replay of **at most `interval`** events. So the interesting numbers are the *tail* --
p95/p99 -- because a p99 far above p50 would mean some keyframe window is larger than the
interval, i.e. a cadence bug in the writer rather than a slow reconstructor. This also
measures the cached sequential step (what a playhead drag actually costs) and the price
of resolving one value.
"""

from __future__ import annotations

import random
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

import io  # noqa: E402

from workloads import WORKLOADS  # type: ignore[import-not-found]  # noqa: E402

from chronotrace.reconstruct import (  # noqa: E402
    KeyframeReconstructor,
    ValueResolver,
    reconstruct_slow,
)
from chronotrace.recorder import MemorySink, Recorder  # noqa: E402
from chronotrace.recorder.scope import Scope  # noqa: E402
from chronotrace.store import ChronoReader  # noqa: E402
from chronotrace.store.keyframe import DEFAULT_KEYFRAME_INTERVAL  # noqa: E402
from chronotrace.store.writer import ChronoWriter  # noqa: E402


def _record(name: str) -> tuple[list, list]:
    fn = WORKLOADS[name]
    fn()
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(include=["*"]))
    with recorder:
        fn()
    return sink.events, recorder._values._values


def _percentiles(samples: list[float]) -> tuple[float, float, float]:
    ordered = sorted(samples)
    at = lambda q: ordered[min(len(ordered) - 1, int(len(ordered) * q))]  # noqa: E731
    return statistics.median(ordered) * 1e6, at(0.95) * 1e6, at(0.99) * 1e6


def main() -> int:
    events, pool = _record("json_pipeline")
    buf = io.BytesIO()
    writer = ChronoWriter(buf)  # tuned defaults (ADR-0005)
    for value in pool:
        writer.add_value(value)
    for event in events:
        writer.add(event)
    writer.close()
    reader = ChronoReader.from_bytes(buf.getvalue())
    n = len(reader)
    print(
        f"json_pipeline: {n:,} events, {reader.keyframe_count():,} keyframes, "
        f"interval {DEFAULT_KEYFRAME_INTERVAL}"
    )

    rng = random.Random(0)  # noqa: S311 -- reproducibility, not security
    targets = [rng.randrange(n) for _ in range(400)]

    # The contract: every target is within one interval of its keyframe.
    replay_depths = []
    for seq in targets:
        kf = reader.nearest_keyframe_at_or_before(seq)
        replay_depths.append(seq - (kf.seq if kf else 0))
    print(
        f"replay depth: max {max(replay_depths):,} events "
        f"(contract: <= {DEFAULT_KEYFRAME_INTERVAL}) -- "
        f"{'HOLDS' if max(replay_depths) <= DEFAULT_KEYFRAME_INTERVAL else 'VIOLATED'}"
    )

    fast = KeyframeReconstructor(reader, use_cache=False)
    cold = []
    for seq in targets:
        t0 = time.perf_counter()
        fast.reconstruct(seq)
        cold.append(time.perf_counter() - t0)
    p50, p95, p99 = _percentiles(cold)
    print(
        f"\nrandom reconstruct (uncached): p50 {p50:7.0f} us  p95 {p95:7.0f} us  p99 {p99:7.0f} us"
    )

    # A playhead drag: one event forward at a time, through the cache.
    cached = KeyframeReconstructor(reader, use_cache=True)
    start = n // 2
    cached.reconstruct(start)
    steps = []
    for seq in range(start + 1, start + 501):
        t0 = time.perf_counter()
        cached.reconstruct(seq)
        steps.append(time.perf_counter() - t0)
    s50, s95, s99 = _percentiles(steps)
    print(
        f"cached +1 step (a drag):        p50 {s50:7.1f} us  p95 {s95:7.1f} us  p99 {s99:7.1f} us"
    )

    # The oracle, for scale: O(seq) rather than O(interval).
    mid = n // 2
    t0 = time.perf_counter()
    reconstruct_slow(reader, mid)
    slow = (time.perf_counter() - t0) * 1e6
    print(
        f"oracle at seq {mid:,} (O(seq)):   {slow:9.0f} us  "
        f"-- {slow / max(p50, 1e-9):.0f}x the fast path, which is the point"
    )

    # Value resolution: the lazy step the UI pays only for what it shows.
    state = fast.reconstruct(mid)
    resolver = ValueResolver(reader)
    refs = [ref for f in state.frames for ref in f.bindings.values()][:200]
    if refs:
        t0 = time.perf_counter()
        for ref in refs:
            resolver.resolve(ref)
        first = (time.perf_counter() - t0) / len(refs) * 1e6
        t0 = time.perf_counter()
        for ref in refs:
            resolver.resolve(ref)
        cachedus = (time.perf_counter() - t0) / len(refs) * 1e6
        print(f"resolve one value:              {first:7.1f} us cold, {cachedus:5.2f} us cached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
