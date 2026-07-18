"""Day 16: bytes per delta, and the price of invertibility (storing `old_ref`).

Run: `python benchmarks/bench_delta.py`

Deltas are derived from a real recording (json_pipeline), encoded columnar, and
zstd-compressed -- the on-disk form. The headline is the marginal cost of the *old*
ref: the bytes we spend to make backward stepping O(1) instead of O(interval). If it
is cheap, invertibility is a free win; if not, we defend it with the backward-step
benefit that day 21 will lean on.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

from workloads import WORKLOADS  # type: ignore[import-not-found]  # noqa: E402

from chronotrace.recorder import MemorySink, Recorder  # noqa: E402
from chronotrace.recorder.scope import Scope  # noqa: E402
from chronotrace.store.compression import compress  # noqa: E402
from chronotrace.store.delta import NO_REF, DeltaKind, derive, encode_deltas  # noqa: E402
from chronotrace.store.keyframe import LiveState  # noqa: E402


def _record(name: str) -> list:
    fn = WORKLOADS[name]
    fn()
    sink = MemorySink()
    with Recorder(sink, capture_values=True, scope=Scope(include=["*"])):
        fn()
    return sink.events


def _derive_all(events: list) -> list:
    live = LiveState()
    deltas = []
    for event in events:
        deltas.extend(derive(event, live.frames))
        live.apply(event)
    return deltas


def main() -> int:
    events = _record("json_pipeline")
    deltas = _derive_all(events)
    kinds = Counter(d.kind for d in deltas)

    raw = len(encode_deltas(deltas))
    comp = len(compress(encode_deltas(deltas)))
    # Drop the varying old ref (set it constant): the size we recover is exactly what
    # invertibility costs, since everything else is identical.
    forward_only = [replace(d, old_ref=NO_REF) if d.kind == DeltaKind.BIND else d for d in deltas]
    comp_fwd = len(compress(encode_deltas(forward_only)))

    n = len(deltas)
    print(f"json_pipeline: {len(events):,} events -> {n:,} deltas")
    print(
        f"  kinds: {kinds[DeltaKind.BIND]:,} bind, "
        f"{kinds[DeltaKind.FRAME_ENTER]:,} enter, {kinds[DeltaKind.FRAME_EXIT]:,} exit"
    )
    print(f"  encoded raw:        {raw:>9,} B  ({raw / n:5.1f} B/delta)")
    print(f"  encoded + zstd:     {comp:>9,} B  ({comp / n:5.2f} B/delta)  <- the on-disk cost")
    print(f"  forward-only + zstd:{comp_fwd:>9,} B  ({comp_fwd / n:5.2f} B/delta)")
    extra = comp - comp_fwd
    print(
        f"  invertibility cost: {extra:>+9,} B  ({extra / n:+5.2f} B/delta, "
        f"{extra / comp_fwd * 100:+.0f}%)  <- the price of O(1) backward steps"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
