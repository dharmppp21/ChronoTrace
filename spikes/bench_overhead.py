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
import gc
import json
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from workloads import WORKLOADS

WORKLOAD_FILE = str(Path(__file__).parent / "workloads.py")

CONDITIONS = [
    "baseline",
    "settrace_noop",
    "mon_line_noop",
    "mon_line_append",
    "mon_line_scoped",
]


# ---------------------------------------------------------------------------
# Tool ID policy
# ---------------------------------------------------------------------------


def acquire_tool_id() -> int:
    """Claim a sys.monitoring tool id without ever stealing one.

    There are only six ids, four of them conventionally spoken for (DEBUGGER=0,
    COVERAGE=1, PROFILER=2, OPTIMIZER=5). ChronoTrace is a debugger, so it wants
    DEBUGGER_ID -- but another tool may already hold it, and a debugger that
    kicks out whatever was there is a debugger that corrupts someone else's
    session. We ask, we fall back, and we fail loudly rather than force it.

    Returns:
        A tool id we now own and must free.

    Raises:
        RuntimeError: every id is taken, naming the holders so the user can act.
    """
    mon = sys.monitoring
    for tool_id in (mon.DEBUGGER_ID, 3, 4):
        if mon.get_tool(tool_id) is None:
            mon.use_tool_id(tool_id, "chronotrace-spike")
            return tool_id
    holders = {i: mon.get_tool(i) for i in range(6)}
    raise RuntimeError(f"no free sys.monitoring tool id; holders={holders}")


# ---------------------------------------------------------------------------
# Conditions. Each returns (seconds, event_count).
# ---------------------------------------------------------------------------


def _timed(fn: Callable[[], Any]) -> float:
    gc.collect()
    gc.disable()
    try:
        t0 = time.perf_counter()
        fn()
        return time.perf_counter() - t0
    finally:
        gc.enable()


def run_baseline(work: Callable[[], Any]) -> tuple[float, int]:
    """No instrumentation at all. The denominator for every other number."""
    return _timed(work), 0


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

    gc.collect()
    gc.disable()
    try:
        sys.settrace(global_tracer)
        t0 = time.perf_counter()
        work()
        elapsed = time.perf_counter() - t0
    finally:
        sys.settrace(None)
        gc.enable()
    return elapsed, count


def _run_monitoring(
    work: Callable[[], Any],
    make_callback: Callable[[], tuple[Callable[..., Any], Callable[[], int]]],
) -> tuple[float, int]:
    mon = sys.monitoring
    tool_id = acquire_tool_id()
    callback, get_count = make_callback()
    try:
        mon.register_callback(tool_id, mon.events.LINE, callback)
        gc.collect()
        gc.disable()
        try:
            mon.set_events(tool_id, mon.events.LINE)
            t0 = time.perf_counter()
            work()
            elapsed = time.perf_counter() - t0
        finally:
            mon.set_events(tool_id, 0)
            gc.enable()
    finally:
        mon.register_callback(tool_id, mon.events.LINE, None)
        mon.free_tool_id(tool_id)
    return elapsed, get_count()


def run_mon_line_noop(work: Callable[[], Any]) -> tuple[float, int]:
    """LINE events with the cheapest possible callback: just count."""

    def make() -> tuple[Callable[..., Any], Callable[[], int]]:
        box = [0]

        def cb(code: Any, line_number: int) -> Any:
            box[0] += 1
            return None

        return cb, lambda: box[0]

    return _run_monitoring(work, make)


def run_mon_line_append(work: Callable[[], Any]) -> tuple[float, int]:
    """LINE events, recording (code_id, lineno) -- the shape of real work.

    Closest condition to what ChronoTrace will actually do, minus value capture.
    """

    def make() -> tuple[Callable[..., Any], Callable[[], int]]:
        sink: list[tuple[int, int]] = []

        def cb(code: Any, line_number: int) -> Any:
            sink.append((id(code), line_number))
            return None

        return cb, lambda: len(sink)

    return _run_monitoring(work, make)


def run_mon_line_scoped(work: Callable[[], Any]) -> tuple[float, int]:
    """The lever: DISABLE every line outside the workload module.

    Out-of-scope locations cost exactly one callback each, ever, instead of one
    per execution. This is the number that decides whether scope filtering is a
    micro-optimisation or the whole ballgame.
    """

    def make() -> tuple[Callable[..., Any], Callable[[], int]]:
        sink: list[tuple[int, int]] = []
        disable = sys.monitoring.DISABLE
        target = WORKLOAD_FILE

        def cb(code: Any, line_number: int) -> Any:
            if code.co_filename != target:
                return disable
            sink.append((id(code), line_number))
            return None

        return cb, lambda: len(sink)

    return _run_monitoring(work, make)


RUNNERS: dict[str, Callable[[Callable[[], Any]], tuple[float, int]]] = {
    "baseline": run_baseline,
    "settrace_noop": run_settrace_noop,
    "mon_line_noop": run_mon_line_noop,
    "mon_line_append": run_mon_line_append,
    "mon_line_scoped": run_mon_line_scoped,
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
    """95th percentile by linear interpolation.

    With small N this is close to the max; that is honest and stated in the
    results rather than dressed up.
    """
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    pos = 0.95 * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


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
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    if args.child:
        child_main(args.child[0], args.child[1])
        return 0

    results: list[dict[str, Any]] = []
    for workload in WORKLOADS:
        for condition in CONDITIONS:
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
        for condition in CONDITIONS:
            row = next(
                r for r in results if r["workload"] == workload and r["condition"] == condition
            )
            ratio = row["median"] / base if base else float("nan")
            print(
                f"{workload:<16} {condition:<18} "
                f"{row['median'] * 1000:>9.2f}ms {ratio:>8.1f}x {row['events']:>12,}"
            )

    if args.json_out:
        args.json_out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
