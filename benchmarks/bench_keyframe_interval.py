"""Day 15: the keyframe interval tradeoff curve -- the README asset.

Run: `python benchmarks/bench_keyframe_interval.py`

Small interval -> bigger file, fewer events to replay to reach any instant.
Large interval -> smaller file, more to replay. Reconstruction touches **at most one
interval** of events -- the product's scrubbing-latency contract -- so this curve is
how you buy latency with bytes. Measured on a real recording (json_pipeline) whose
values go through the day-14 pool, so keyframes store refs, not values.
"""

from __future__ import annotations

import io
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
from chronotrace.store import ChronoReader  # noqa: E402
from chronotrace.store.compression import compress  # noqa: E402
from chronotrace.store.keyframe import LiveState  # noqa: E402
from chronotrace.store.writer import ChronoWriter  # noqa: E402

_INTERVALS = (100, 1_000, 10_000, 100_000)


def _record(name: str) -> tuple[list, list]:
    fn = WORKLOADS[name]
    fn()
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(include=["*"]))
    with recorder:
        fn()
    return sink.events, recorder._values._values


def _write(events: list, pool_values: list, *, interval: int) -> bytes:
    buf = io.BytesIO()
    writer = ChronoWriter(buf, keyframe_interval=interval)
    for value in pool_values:
        writer.add_value(value)
    for event in events:
        writer.add(event)
    writer.close()
    return buf.getvalue()


def _lookup_us(reader: ChronoReader, n_events: int) -> float:
    import random

    rng = random.Random(0)  # noqa: S311 -- reproducibility, not security
    samples = [rng.randrange(n_events) for _ in range(2000)]
    runs = []
    for seq in samples:
        t0 = time.perf_counter()
        reader.nearest_keyframe_at_or_before(seq)
        runs.append(time.perf_counter() - t0)
    return statistics.median(runs) * 1e6


def _representative_keyframe(events: list) -> tuple[int, int]:
    """Raw and compressed size of the deepest keyframe (all events applied)."""
    live = LiveState()
    for event in events:
        live.apply(event)
    raw = live.encode()
    return len(raw), len(compress(raw))


def main() -> int:
    events, pool_values = _record("json_pipeline")
    n = len(events)
    baseline = len(_write(events, pool_values, interval=10 * n + 1))  # one keyframe: the floor
    kf_raw, kf_comp = _representative_keyframe(events)

    print(f"json_pipeline: {n:,} events, {len(pool_values):,} pooled values")
    print(f"representative keyframe: {kf_raw:,} B raw, {kf_comp:,} B compressed (refs, not values)")
    print(f"baseline file (1 keyframe): {baseline / 1024:.1f} KiB\n")

    print(
        f"{'interval':>9} {'keyframes':>10} {'file KiB':>9} {'overhead':>9} "
        f"{'B/kf':>7} {'lookup us':>10} {'max replay':>11}"
    )
    print("-" * 72)
    rows = []
    for interval in _INTERVALS:
        data = _write(events, pool_values, interval=interval)
        reader = ChronoReader.from_bytes(data)
        kfs = reader.keyframe_count()
        overhead = (len(data) - baseline) / baseline * 100
        per_kf = (len(data) - baseline) / max(kfs - 1, 1)
        lookup = _lookup_us(reader, n)
        rows.append((interval, len(data) / 1024, overhead))
        print(
            f"{interval:>9,} {kfs:>10,} {len(data) / 1024:>8.1f}K {overhead:>8.1f}% "
            f"{per_kf:>7.0f} {lookup:>9.2f}u {min(interval, n):>11,}"
        )

    print("\nfile size vs interval (smaller interval == bigger file, faster seek):")
    lo = min(r[1] for r in rows)
    hi = max(r[1] for r in rows)
    for interval, kib, _ in rows:
        bar = "#" * (4 + int(36 * (kib - lo) / (hi - lo + 1e-9)))
        print(f"  {interval:>7,} | {bar} {kib:.0f}K")
    print("\nContract: reaching any seq replays AT MOST `interval` events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
