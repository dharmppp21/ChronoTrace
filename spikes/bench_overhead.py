"""Measure the cost of observing every line of a Python program.

The question this spike exists to answer: can we watch every line of a program at
a price a developer will actually pay? Everything in ChronoTrace rests on the
answer, so the methodology matters more than the code.

Why every measurement runs in a fresh subprocess
------------------------------------------------
This is the one thing that would silently invalidate the whole benchmark.

``sys.monitoring`` instrumentation is **process-global and sticky**. Returning
``DISABLE`` from a callback permanently de-instruments that code location for the
lifetime of the process, and only ``restart_events()`` -- which re-enables
*everything*, for *every tool* -- undoes it. So if the "scoped" condition ran
before the "no-op" condition in one process, the no-op condition would inherit a
partially de-instrumented interpreter and look impossibly fast. CPython's
adaptive specialisation warms code objects across runs too, and ``settrace`` and
``sys.monitoring`` interfere with each other.

One process per measurement makes each number independent by construction. It
costs ~100ms of interpreter startup per sample, which lands entirely outside the
timed region and therefore does not touch the result.

Methodology
-----------
* **Warmup**: each child runs the workload once *uninstrumented* before timing.
  This pays for imports, disk cache and CPU frequency ramp. It deliberately does
  not warm the instrumented path -- a real recording gets no warmup either.
* **One timed run per process**, repeated across processes.
* **Median and p95, never best-of-N.** Best-of-N reports the luckiest scheduling
  accident on the machine; the median reports what a user gets.
* **GC collected then disabled around the timed region.** See the honest caveat
  in ``RESULTS-overhead.md``: this trades realism for stability, and it
  *understates* the append condition specifically.
* **Event counts are reported and asserted non-zero.** A benchmark whose callback
  silently never fires shows beautiful overhead numbers and measures nothing.
  This is the classic way to fool yourself, so it is checked rather than assumed.

Usage::

    python spikes/bench_overhead.py               # full matrix
    python spikes/bench_overhead.py --reps 3
    python spikes/bench_overhead.py --child mon_line_noop tight_loop   # internal
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import json
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import spike_capture
from workloads import WORKLOADS

WORKLOAD_FILE = str(Path(__file__).parent / "workloads.py")

# PEP 669 reserves 0/1/2/5 for debugger/coverage/profiler/optimizer and leaves
# 3 and 4 free. The real recorder's acquire-and-fall-back policy is a day 5
# product decision and is argued in RESULTS-overhead.md; it does not belong in
# throwaway code.
TOOL_ID = 3


# ---------------------------------------------------------------------------
# Conditions. Each returns (seconds, event_count).
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _no_gc() -> Iterator[None]:
    """Collect, then hold GC off across the timed region.

    Buys stability, costs realism -- and understates the append condition
    specifically, whose 750k allocations are exactly what would trigger a
    collection. Stated in RESULTS-overhead.md rather than hidden; day 3
    re-measures with GC live.
    """
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


def run_baseline(work: Callable[[], Any]) -> tuple[float, int]:
    """No instrumentation at all. The denominator for every other number."""
    with _no_gc():
        t0 = time.perf_counter()
        work()
        return time.perf_counter() - t0, 0


def run_settrace_noop(work: Callable[[], Any]) -> tuple[float, int]:
    """The classic mechanism: one global trace function, every line, everywhere.

    The local tracer must return itself or CPython stops delivering line events
    for that frame -- returning None here would measure call-only tracing and
    quietly understate settrace by an order of magnitude.
    """
    count = 0

    def local_tracer(frame: Any, event: str, arg: Any) -> Any:
        nonlocal count
        count += 1
        return local_tracer

    def global_tracer(frame: Any, event: str, arg: Any) -> Any:
        return local_tracer

    with _no_gc():
        try:
            sys.settrace(global_tracer)
            t0 = time.perf_counter()
            work()
            elapsed = time.perf_counter() - t0
        finally:
            sys.settrace(None)
    return elapsed, count


def _run_monitoring(
    work: Callable[[], Any],
    callback: Callable[..., Any],
    get_count: Callable[[], int],
    *,
    gc_live: bool = False,
) -> tuple[float, int]:
    """Time `work` with `callback` on LINE events.

    Args:
        work: the workload.
        callback: the LINE handler.
        get_count: reports how many events the callback saw.
        gc_live: leave GC running across the timed region. The capture
            conditions set this: capture allocates, and allocation is exactly
            what triggers collection, so suppressing GC would flatter the two
            conditions whose realism matters most.
    """
    mon = sys.monitoring
    mon.use_tool_id(TOOL_ID, "chronotrace-spike")
    gc_ctx = contextlib.nullcontext() if gc_live else _no_gc()
    try:
        mon.register_callback(TOOL_ID, mon.events.LINE, callback)
        if gc_live:
            gc.collect()
        with gc_ctx:
            try:
                mon.set_events(TOOL_ID, mon.events.LINE)
                t0 = time.perf_counter()
                work()
                elapsed = time.perf_counter() - t0
            finally:
                mon.set_events(TOOL_ID, 0)
    finally:
        mon.register_callback(TOOL_ID, mon.events.LINE, None)
        mon.free_tool_id(TOOL_ID)
    return elapsed, get_count()


def run_mon_line_noop(work: Callable[[], Any]) -> tuple[float, int]:
    """LINE events with the cheapest possible callback: just count."""
    count = 0

    def cb(code: Any, line_number: int) -> Any:
        nonlocal count
        count += 1
        return None

    return _run_monitoring(work, cb, lambda: count)


def run_mon_line_append(work: Callable[[], Any]) -> tuple[float, int]:
    """LINE events, recording (code_id, lineno) -- the shape of real work.

    Closest condition to what ChronoTrace will actually do, minus value capture.
    """
    sink: list[tuple[int, int]] = []

    def cb(code: Any, line_number: int) -> Any:
        sink.append((id(code), line_number))
        return None

    return _run_monitoring(work, cb, lambda: len(sink))


def run_mon_line_scoped(work: Callable[[], Any]) -> tuple[float, int]:
    """The lever: DISABLE every line outside the workload module.

    Out-of-scope locations cost exactly one callback each, ever, instead of one
    per execution. This is the number that decides whether scope filtering is a
    micro-optimisation or the whole ballgame.
    """
    sink: list[tuple[int, int]] = []
    disable = sys.monitoring.DISABLE
    target = WORKLOAD_FILE

    def cb(code: Any, line_number: int) -> Any:
        if code.co_filename != target:
            return disable
        sink.append((id(code), line_number))
        return None

    return _run_monitoring(work, cb, lambda: len(sink))


def run_mon_capture_scoped(work: Callable[[], Any]) -> tuple[float, int]:
    """The honest combined figure: scoping + real value capture of frame locals.

    Every other condition measures the *floor* -- appending a tuple is not
    recording. This one captures the frame's locals through the day-3 spike
    capturer, which is what the product will actually do. It is the number
    ADR-0001 turns on.

    GC stays live here, unlike the other conditions: capture allocates, and
    allocation is exactly what triggers collection. Suppressing GC would flatter
    the one condition whose realism matters most.
    """
    sink: list[Any] = []
    disable = sys.monitoring.DISABLE
    target = WORKLOAD_FILE
    cap = spike_capture.capture

    def cb(code: Any, line_number: int) -> Any:
        if code.co_filename != target:
            return disable
        frame = sys._getframe(1)
        sink.append({k: cap(v) for k, v in frame.f_locals.items()})
        return None

    return _run_monitoring(work, cb, lambda: len(sink), gc_live=True)


def run_mon_capture_changed(work: Callable[[], Any]) -> tuple[float, int]:
    """Capture only bindings whose value identity changed since the last event.

    Tests the day-8 hypothesis directly, because ADR-0001 cannot bet on a fix
    nobody has measured. `mon_capture_scoped` re-captures every local on every
    line -- a 1200-element list gets walked 13,210 times though it never changes
    after line one. This skips the unchanged ones.

    **This is an optimistic bound, not a shippable design.** Identity is an
    unsound proxy for "unchanged": `lst.append(x)` mutates in place and keeps the
    same id, so this would miss the write and show the user stale state -- the
    worst failure a debugger has. Day 8 makes it sound by restricting the
    identity shortcut to immutable types and re-capturing mutables. That will
    cost more than this measures; this is the ceiling, and the honest figure sits
    between this and `mon_capture_scoped`.
    """
    sink: list[Any] = []
    last: dict[tuple[int, str], int] = {}
    disable = sys.monitoring.DISABLE
    target = WORKLOAD_FILE
    cap = spike_capture.capture

    def cb(code: Any, line_number: int) -> Any:
        if code.co_filename != target:
            return disable
        frame = sys._getframe(1)
        fid = id(frame)
        for k, v in frame.f_locals.items():
            key = (fid, k)
            vid = id(v)
            if last.get(key) != vid:
                last[key] = vid
                sink.append(cap(v))
        return None

    return _run_monitoring(work, cb, lambda: len(sink), gc_live=True)


RUNNERS: dict[str, Callable[[Callable[[], Any]], tuple[float, int]]] = {
    "baseline": run_baseline,
    "settrace_noop": run_settrace_noop,
    "mon_line_noop": run_mon_line_noop,
    "mon_line_append": run_mon_line_append,
    "mon_line_scoped": run_mon_line_scoped,
    "mon_capture_scoped": run_mon_capture_scoped,
    "mon_capture_changed": run_mon_capture_changed,
}


# ---------------------------------------------------------------------------
# Child: one condition, one workload, one timed run.
# ---------------------------------------------------------------------------


def child_main(condition: str, workload_name: str) -> None:
    work = WORKLOADS[workload_name]
    work()  # warmup: imports, disk cache, cpu ramp -- uninstrumented on purpose
    seconds, events = RUNNERS[condition](work)
    print(json.dumps({"seconds": seconds, "events": events}))


# ---------------------------------------------------------------------------
# Parent: orchestrate, aggregate, report.
# ---------------------------------------------------------------------------


def _p95(values: list[float]) -> float:
    """95th percentile. With small N this is close to the max; stated in the results."""
    if len(values) < 2:
        return values[0]
    return statistics.quantiles(values, n=20, method="inclusive")[18]


def measure(condition: str, workload: str, reps: int) -> dict[str, Any]:
    times: list[float] = []
    events = 0
    for _ in range(reps):
        proc = subprocess.run(
            [sys.executable, __file__, "--child", condition, workload],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        times.append(payload["seconds"])
        events = payload["events"]
    return {
        "condition": condition,
        "workload": workload,
        "median": statistics.median(times),
        "p95": _p95(times),
        "events": events,
        "samples": times,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", nargs=2, metavar=("CONDITION", "WORKLOAD"))
    parser.add_argument("--reps", type=int, default=7)
    args = parser.parse_args()

    if args.child:
        child_main(args.child[0], args.child[1])
        return 0

    results: list[dict[str, Any]] = []
    for workload in WORKLOADS:
        for condition in RUNNERS:
            row = measure(condition, workload, args.reps)
            results.append(row)
            print(
                f"{workload:<16} {condition:<18} "
                f"median={row['median'] * 1000:9.2f}ms  "
                f"p95={row['p95'] * 1000:9.2f}ms  "
                f"events={row['events']:>10,}",
                flush=True,
            )

    print("\n" + "=" * 78)
    print(f"{'workload':<16} {'condition':<18} {'median':>10} {'vs base':>9} {'events':>12}")
    print("=" * 78)
    for workload in WORKLOADS:
        base = next(
            r["median"]
            for r in results
            if r["workload"] == workload and r["condition"] == "baseline"
        )
        for condition in RUNNERS:
            row = next(
                r for r in results if r["workload"] == workload and r["condition"] == condition
            )
            ratio = row["median"] / base if base else float("nan")
            print(
                f"{workload:<16} {condition:<18} "
                f"{row['median'] * 1000:>9.2f}ms {ratio:>8.1f}x {row['events']:>12,}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
