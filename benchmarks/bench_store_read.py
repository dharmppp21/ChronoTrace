"""Read-path performance (day 13): random-access latency and lazy open.

Run: `python benchmarks/bench_store_read.py`

Writes a 1M-event `.chrono`, then measures what the timeline scrubber will feel:
per-`__getitem__` latency sequentially (the dominant pattern, mostly LRU hits) and
at random (each a fresh block decode). Also confirms that *opening* the file does
not decode it -- the whole point of mmap + lazy blocks.
"""

from __future__ import annotations

import random
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from chronotrace.recorder.events import Event, EventKind  # noqa: E402
from chronotrace.store import ChronoReader, FileSink  # noqa: E402

_N = 1_000_000


def _write(path: Path) -> None:
    sink = FileSink(path)
    for seq in range(_N):
        sink.emit(
            Event(
                seq=seq,
                kind=EventKind.LINE,
                timestamp_ns=1000 + seq,
                thread_id=1,
                frame_id=seq // 50,
                code_id=1,
                lineno=seq % 40,
            )
        )
    sink.close()


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "big.chrono"
        _write(path)
        file_mb = path.stat().st_size / 1e6

        tracemalloc.start()
        reader = ChronoReader.open(path)
        open_kib = tracemalloc.get_traced_memory()[1] / 1024
        tracemalloc.stop()

        # Sequential: adjacent seqs, so most land in the cached block.
        t0 = time.perf_counter()
        for seq in range(100_000):
            reader[seq]
        seq_us = (time.perf_counter() - t0) / 100_000 * 1e6

        # Random: each access likely a fresh block decode.
        rng = random.Random(0)  # noqa: S311  -- reproducibility, not security
        samples = [rng.randrange(_N) for _ in range(3000)]
        runs = []
        for seq in samples:
            t0 = time.perf_counter()
            reader[seq]
            runs.append(time.perf_counter() - t0)
        runs.sort()

        reader.close()

    ratio = file_mb * 1024 / open_kib
    r_med = runs[len(runs) // 2] * 1e6
    r_p95 = runs[int(len(runs) * 0.95)] * 1e6
    print(
        f"file:            {file_mb:6.1f} MB for {_N:,} events ({file_mb * 1e6 / _N:.1f} B/event)"
    )
    print(f"open (lazy):     {open_kib:6.1f} KiB heap -- {ratio:.0f}x smaller than the file")
    print(f"sequential get:  {seq_us:6.2f} us/getitem  (LRU-warm; the scrubber's path)")
    print(f"random get:      {r_med:6.1f} us median, {r_p95:.0f} us p95  (cold block decode)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
