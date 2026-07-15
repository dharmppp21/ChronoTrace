"""Measure the event model's cost, and decide AoS vs SoA with a number.

Run: `python benchmarks/bench_events.py`

The methodology trap this benchmark exists to avoid
---------------------------------------------------
`tracemalloc` instruments **every allocation**. It therefore taxes the design that
allocates a million objects (array-of-structures) far more than the one that
allocates none (structure-of-arrays). Measuring both at once reported SoA as 3.3x
*faster* than AoS. Timed separately, AoS is faster.

That single mistake would have chosen the wrong representation for the whole
project, so time and memory are measured in separate passes here and the reason is
written down rather than remembered.
"""

from __future__ import annotations

import array
import gc
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

N = 1_000_000


@dataclass(slots=True, frozen=True)
class EvSlots:
    seq: int
    kind: int
    ts: int
    thread_id: int
    frame_id: int
    code_id: int
    lineno: int


@dataclass(frozen=True)
class EvNoSlots:
    seq: int
    kind: int
    ts: int
    thread_id: int
    frame_id: int
    code_id: int
    lineno: int


class EvNamed(NamedTuple):
    seq: int
    kind: int
    ts: int
    thread_id: int
    frame_id: int
    code_id: int
    lineno: int


class AoSSink:
    """What we shipped: one object per event, appended to a list."""

    def __init__(self) -> None:
        self._e: list[Any] = []

    def emit(self, ev: Any) -> None:
        self._e.append(ev)


class SoASink:
    """The rejected alternative: emit takes fields, columns hold raw int64."""

    __slots__ = ("cid", "fid", "kind", "line", "seq", "tid", "ts")

    def __init__(self) -> None:
        for f in self.__slots__:
            setattr(self, f, array.array("q"))

    def emit(self, seq: int, kind: int, ts: int, tid: int, fid: int, cid: int, line: int) -> None:
        self.seq.append(seq)
        self.kind.append(kind)
        self.ts.append(ts)
        self.tid.append(tid)
        self.fid.append(fid)
        self.cid.append(cid)
        self.line.append(line)


def _fields(i: int) -> tuple[int, int, int, int, int, int, int]:
    return (i, 1, i, 1, i % 100, i % 50, i % 900)


def build_aos_slots() -> Any:
    s = AoSSink()
    for i in range(N):
        s.emit(EvSlots(*_fields(i)))
    return s


def build_aos_noslots() -> Any:
    s = AoSSink()
    for i in range(N):
        s.emit(EvNoSlots(*_fields(i)))
    return s


def build_aos_named() -> Any:
    s = AoSSink()
    for i in range(N):
        s.emit(EvNamed(*_fields(i)))
    return s


def build_aos_tuple() -> Any:
    s = AoSSink()
    for i in range(N):
        s.emit(_fields(i))
    return s


def build_soa() -> Any:
    s = SoASink()
    for i in range(N):
        s.emit(*_fields(i))
    return s


def time_ns_per_event(build: Callable[[], Any]) -> float:
    """Median of 3, with no tracemalloc active. See the module docstring."""
    runs = []
    for _ in range(3):
        gc.collect()
        t0 = time.perf_counter()
        keep = build()
        runs.append(time.perf_counter() - t0)
        del keep
    runs.sort()
    return runs[1] / N * 1e9


def bytes_per_event(build: Callable[[], Any]) -> float:
    """Separate pass: tracemalloc distorts timing, so it never runs alongside it."""
    gc.collect()
    tracemalloc.start()
    keep = build()
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del keep
    return current / N


def main() -> int:
    designs: list[tuple[str, Callable[[], Any]]] = [
        ("AoS dataclass(slots=True)  [SHIPPED]", build_aos_slots),
        ("AoS dataclass(no slots)", build_aos_noslots),
        ("AoS NamedTuple", build_aos_named),
        ("AoS plain tuple (no names, no types)", build_aos_tuple),
        ("SoA array.array('q')", build_soa),
    ]
    print(f"{'design':<38} {'ns/event':>9} {'B/event':>9} {'MB @ 1M':>9}")
    print("-" * 68)
    for label, build in designs:
        ns = time_ns_per_event(build)
        b = bytes_per_event(build)
        print(f"{label:<38} {ns:>9.0f} {b:>9.1f} {b * N / 1e6:>9.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
