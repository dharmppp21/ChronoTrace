"""Day 21: what backward stepping costs, and what the rejected design would have cost.

Run: `python benchmarks/bench_stepping.py`

Two questions:

1. **Is a backward drag interactive?** A step is a `seq` search plus one `reconstruct`, so
   its cost is the search distance plus the bounded replay. The budget is a 60 fps frame,
   16,000 us.
2. **Was rejecting the incremental backward path right?** ADR-0006 (b) assumed the delta
   replay dominates a backward step. This measures the split, because an optimisation that
   can only remove 29% of a 240 us operation does not earn a second state machine.
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

from chronotrace.reconstruct import (  # noqa: E402
    Direction,
    Edge,
    KeyframeReconstructor,
    step,
    step_out,
    step_over,
)
from chronotrace.reconstruct._replay import (  # noqa: E402
    apply_deltas,
    freeze,
    overlay_events,
    work_from_keyframe,
)
from chronotrace.recorder import MemorySink, Recorder  # noqa: E402
from chronotrace.recorder.events import EventKind  # noqa: E402
from chronotrace.recorder.scope import Scope  # noqa: E402
from chronotrace.store import ChronoReader, ChronoWriter  # noqa: E402

BACK = Direction.BACKWARD
FRAME_BUDGET_US = 16_000  # one frame at 60 fps


def _recording() -> ChronoReader:
    fn = WORKLOADS["json_pipeline"]
    fn()  # warm the interpreter so the recorded run is steady-state
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(include=["*"]))
    with recorder:
        fn()
    buf = io.BytesIO()
    writer = ChronoWriter(buf)  # tuned defaults (ADR-0005)
    for captured in recorder.values:
        writer.add_value(captured)
    for event in sink.events:
        writer.add(event)
    writer.close()
    return ChronoReader.from_bytes(buf.getvalue())


def _percentiles(samples: list[float]) -> tuple[float, float, float]:
    ordered = sorted(samples)
    at = lambda q: ordered[min(len(ordered) - 1, int(len(ordered) * q))]  # noqa: E731
    return statistics.median(ordered) * 1e6, at(0.95) * 1e6, at(0.99) * 1e6


def _drag(reader: ChronoReader, limit: int) -> list[float]:
    """A backward drag: `step_back` then reconstruct, exactly what the REPL's `p` does."""
    recon = KeyframeReconstructor(reader)
    seq: int | Edge = len(reader) - 1
    recon.reconstruct(seq)
    samples: list[float] = []
    while len(samples) < limit:
        start = time.perf_counter()
        seq = step(reader, seq, BACK)
        if isinstance(seq, Edge):
            break
        recon.reconstruct(seq)
        samples.append(time.perf_counter() - start)
    return samples


def _distances(reader: ChronoReader) -> None:
    """How far each operation actually travels -- the number that killed the state walk."""
    events = reader[0 : len(reader)]
    last_line: dict[int, int] = {}
    any_gap: list[int] = []
    frame_gap: list[int] = []
    previous = None
    for event in events:  # type: ignore[union-attr]
        if event.kind != EventKind.LINE:
            continue
        if previous is not None:
            any_gap.append(event.seq - previous)
        previous = event.seq
        seen = last_line.get(event.frame_id)
        if seen is not None:
            frame_gap.append(event.seq - seen)
        last_line[event.frame_id] = event.seq
    print(
        f"step_back      distance: p50 {statistics.median(any_gap):.0f}  max {max(any_gap):,}\n"
        f"step_over_back distance: p50 {statistics.median(frame_gap):.0f}  "
        f"max {max(frame_gap):,}   <- deltas a state walk would invert for ONE command"
    )


def _cost_split(reader: ChronoReader) -> None:
    """Where a backward step's time goes: the replay ADR-0006(b) targets, and the rest."""
    deltas: list[float] = []
    overlay: list[float] = []
    for seq in range(len(reader) // 2, len(reader) // 2 + 200):
        keyframe = reader.nearest_keyframe_at_or_before(seq)
        if keyframe is None:
            continue
        work = work_from_keyframe(keyframe)
        t0 = time.perf_counter()
        apply_deltas(work, reader.deltas_between(keyframe.seq + 1, seq))
        t1 = time.perf_counter()
        overlay_events(work, reader[keyframe.seq + 1 : seq + 1])  # type: ignore[arg-type]
        t2 = time.perf_counter()
        freeze(work, seq, 0)
        deltas.append(t1 - t0)
        overlay.append(t2 - t1)
    delta_us, over_us = statistics.median(deltas) * 1e6, statistics.median(overlay) * 1e6
    print(
        f"\ndelta replay  {delta_us:7.1f} us  <- all that inverting deltas could remove\n"
        f"event overlay {over_us:7.1f} us  <- not invertible: events carry no old lineno\n"
        f"inversion's ceiling: {delta_us / (delta_us + over_us):.0%} of a backward step"
    )


def main() -> int:
    reader = _recording()
    print(f"json_pipeline: {len(reader):,} events, {reader.keyframe_count():,} keyframes\n")
    _distances(reader)

    samples = _drag(reader, 10_000)
    p50, p95, p99 = _percentiles(samples)
    total = sum(samples)
    print(
        f"\nbackward drag ({len(samples):,} steps): p50 {p50:6.1f} us  p95 {p95:6.1f} us  "
        f"p99 {p99:6.1f} us  total {total:.2f} s\n"
        f"  {'WITHIN' if p99 < FRAME_BUDGET_US else 'OVER'} the {FRAME_BUDGET_US:,} us "
        f"frame budget at p99 -- {FRAME_BUDGET_US / p99:.0f}x headroom"
    )

    mid = len(reader) // 2
    for name, operation in (("step", step), ("step_over", step_over), ("step_out", step_out)):
        timings = []
        for seq in range(mid, mid + 200):
            t0 = time.perf_counter()
            operation(reader, seq, BACK)
            timings.append(time.perf_counter() - t0)
        s50, _s95, s99 = _percentiles(timings)
        print(f"{name + '_back':16s} search only: p50 {s50:7.1f} us  p99 {s99:7.1f} us")

    _cost_split(reader)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
