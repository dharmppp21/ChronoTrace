"""Write throughput of ChronoWriter (day 12): events/s, MB/s, bytes/event.

Run: `python benchmarks/bench_store_write.py`

The events come from recording a real workload (not synthetic best-case data), so
the column distributions -- and thus the compression and the write cost -- are
realistic. Writing to `io.BytesIO` isolates encoding+framing speed from the disk;
a single real-file write with the close-time fsync gives the on-disk number.
"""

from __future__ import annotations

import io
import statistics
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

from workloads import WORKLOADS  # type: ignore[import-not-found]  # noqa: E402

from chronotrace.recorder import MemorySink, Recorder  # noqa: E402
from chronotrace.recorder.scope import Scope  # noqa: E402
from chronotrace.store.writer import ChronoWriter, FileSink  # noqa: E402


def _record(name: str) -> list:
    fn = WORKLOADS[name]
    fn()
    sink = MemorySink()
    with Recorder(sink, capture_values=False, scope=Scope(include=["*"])):
        fn()
    return sink.events


def _write_to_memory(events: list) -> tuple[float, int]:
    buf = io.BytesIO()
    t0 = time.perf_counter()
    writer = ChronoWriter(buf)
    for event in events:
        writer.add(event)
    writer.close()
    return time.perf_counter() - t0, len(buf.getvalue())


def main() -> int:
    print(
        f"{'workload':<15} {'events':>10} {'MB out':>8} "
        f"{'B/event':>8} {'Mevents/s':>10} {'MB/s':>8}"
    )
    print("-" * 64)
    for name in ("tight_loop", "json_pipeline", "fib_recursive"):
        events = _record(name)
        samples = [_write_to_memory(events) for _ in range(5)]
        dt = statistics.median(dt for dt, _ in samples)
        size = samples[0][1]
        n = len(events)
        print(
            f"{name:<15} {n:>10,} {size / 1e6:>7.2f}M {size / n:>8.2f} "
            f"{n / dt / 1e6:>10.2f} {size / dt / 1e6:>8.1f}"
        )

    # One real-file write, fsync included, for the on-disk figure.
    events = _record("tight_loop")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "bench.chrono"
        t0 = time.perf_counter()
        sink = FileSink(path)
        for event in events:
            sink.emit(event)
        sink.close()
        dt = time.perf_counter() - t0
        size = path.stat().st_size
    print(
        f"\nreal file + fsync: {len(events):,} events, {size / 1e6:.2f} MB, "
        f"{len(events) / dt / 1e6:.2f} Mevents/s, {size / dt / 1e6:.1f} MB/s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
