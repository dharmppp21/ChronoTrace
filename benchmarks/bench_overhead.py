"""The headline benchmark: recording overhead against the tools people actually use.

This is the number an interviewer asks about, so the methodology is the point.

Fair baselines, escalating work
-------------------------------
Each condition does *strictly more* than the one above it, and the table says so
rather than pretending a like-for-like race:

* **baseline**        -- no instrumentation. The denominator.
* **settrace no-op**  -- one global trace function, every line, does nothing. The
                         classic ``sys.settrace`` mechanism at its cheapest.
* **pdb (never-hit)** -- ``pdb`` running with a breakpoint set whose condition is
                         always false. This is what a developer *actually* pays
                         when they leave pdb attached: full per-line breakpoint
                         dispatch, no interaction.
* **coverage.py**     -- a production-grade tracer. The fairest external
                         comparison, because it is a real, maintained tool -- but
                         it records only *which lines ran* (a set), which is far
                         less than ChronoTrace records. Do not read a ChronoTrace
                         win over coverage as a per-event speed win.
* **chronotrace flow**    -- the real recorder, ``capture_values=False``: the full
                         ordered event stream (every LINE/CALL/RETURN), scoped to
                         the target's own code. More than coverage: order, not a set.
* **chronotrace +capture** -- the real recorder capturing every local's *value* on
                         every line. Far more than any baseline above; this is the
                         feature, and the honest cost of it.

Why every sample runs in a fresh subprocess
-------------------------------------------
``sys.monitoring`` is process-global and sticky, and both coverage.py *and*
ChronoTrace acquire monitoring tool ids and return ``DISABLE`` to de-instrument
locations permanently. Two conditions in one process would contaminate each other
(a de-instrumented location stays de-instrumented; a leftover tool id collides).
One process per measurement makes every number independent by construction. The
~100 ms of interpreter startup lands entirely outside the timed region.

Scope is a real feature, not a thumb on the scale
-------------------------------------------------
ChronoTrace records only the target's own code (``Scope`` defaults to the script's
tree; stdlib/site-packages are ``DISABLE``d). The tracing baselines have no such
notion and trace everything. That is a genuine structural advantage of PEP 669,
argued in ``spikes/RESULTS-overhead.md`` -- not a measurement trick. The *separate*
fairness caveat is per-event work (capture vs. a line set), stated above.

``--sample`` is NOT measured here: it does not exist yet (issue #18, deferred
behind the empty META block, see ADR-0014). A planned row is never a measured
number, so it is annotated in the rendered table, never fabricated in this data.

Usage::

    python benchmarks/bench_overhead.py                 # full matrix -> table
    python benchmarks/bench_overhead.py --reps 5
    python benchmarks/bench_overhead.py --child chrono_capture tight_loop  # internal
"""

from __future__ import annotations

import argparse
import dis
import gc
import io
import json
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

from workloads import WORKLOADS  # noqa: E402

WORKLOAD_FILE = str((_ROOT / "spikes" / "workloads.py").resolve())
WORKLOAD_DIR = str((_ROOT / "spikes").resolve())

# Order matters only for readable output; each sample is independent (own process).
CONDITIONS = (
    "baseline",
    "settrace_noop",
    "pdb_unhit",
    "coverage_py",
    "chrono_flow",
    "chrono_capture",
)

# Human labels for the rendered table (rendering lives in __main__).
LABELS = {
    "baseline": "baseline (no instrumentation)",
    "settrace_noop": "sys.settrace no-op",
    "pdb_unhit": "pdb (never-hit breakpoint)",
    "coverage_py": "coverage.py",
    "chrono_flow": "chronotrace (flow only)",
    "chrono_capture": "chronotrace (+value capture)",
}


def _gc_settled() -> None:
    """Collect once so a stray collection does not land inside one timed region.

    GC is then left *enabled* for every condition -- disabling it would flatter the
    allocation-heavy capture condition, which is the one whose realism matters most.
    """
    gc.collect()


# ---------------------------------------------------------------------------
# Conditions. Each returns (seconds, events). events == -1 means "not counted".
# ---------------------------------------------------------------------------


def run_baseline(work: Callable[[], Any]) -> tuple[float, int]:
    _gc_settled()
    t0 = time.perf_counter()
    work()
    return time.perf_counter() - t0, 0


def run_settrace_noop(work: Callable[[], Any]) -> tuple[float, int]:
    """A local tracer that returns itself -- else CPython stops sending line events."""
    count = 0

    def tracer(frame: Any, event: str, arg: Any) -> Any:
        nonlocal count
        count += 1
        return tracer

    _gc_settled()
    try:
        sys.settrace(tracer)
        t0 = time.perf_counter()
        work()
        elapsed = time.perf_counter() - t0
    finally:
        sys.settrace(None)
    return elapsed, count


def _breakable_line(work: Callable[[], Any]) -> int:
    """A line inside `work` that certainly holds code, so `set_break` accepts it.

    A breakpoint only needs to *exist* to keep pdb tracing every line (bdb removes
    the trace function when `self.breaks` is empty); it never has to be reached.
    """
    # 3.14's findlinestarts yields (offset, None) for line-less entries -- skip them.
    return max(line for _, line in dis.findlinestarts(work.__code__) if line is not None)


def run_pdb_unhit(work: Callable[[], Any]) -> tuple[float, int]:
    """Trace with pdb under a breakpoint whose condition is always false.

    Measures pdb's real per-line dispatch (`break_here`, condition eval) -- the cost
    of leaving pdb attached -- while `set_continue` + a false condition mean it never
    stops. The `user_*`/`interaction` overrides neutralise any stop entirely, so no
    EOF-on-stdin `BdbQuit` can escape and no wall-time is spent interacting.
    """
    import pdb

    class _SilentPdb(pdb.Pdb):
        def user_line(self, frame: Any) -> None: ...
        def user_call(self, frame: Any, argument_list: Any) -> None: ...
        def user_return(self, frame: Any, return_value: Any) -> None: ...
        def interaction(self, *args: Any) -> None: ...

    dbg = _SilentPdb(stdin=io.StringIO(), stdout=io.StringIO())
    dbg.reset()
    dbg.set_break(work.__code__.co_filename, _breakable_line(work), cond="False")
    dbg.set_continue()  # run freely; a breakpoint exists, so tracing stays installed
    _gc_settled()
    try:
        sys.settrace(dbg.trace_dispatch)
        t0 = time.perf_counter()
        work()
        elapsed = time.perf_counter() - t0
    finally:
        sys.settrace(None)
    return elapsed, -1  # pdb exposes no cheap per-line count; traffic == settrace's


def run_coverage_py(work: Callable[[], Any]) -> tuple[float, int]:
    """coverage.py start/stop around the workload. No data file is written."""
    import coverage

    cov = coverage.Coverage()
    _gc_settled()
    try:
        cov.start()
        t0 = time.perf_counter()
        work()
        elapsed = time.perf_counter() - t0
    finally:
        cov.stop()
    return elapsed, -1  # coverage stores a line *set*, not an event count


def _run_chrono(work: Callable[[], Any], *, capture: bool) -> tuple[float, int]:
    from chronotrace.recorder import MemorySink, Recorder
    from chronotrace.recorder.scope import Scope

    sink = MemorySink()
    # Scope to the target's own tree -- exactly what the CLI does for a real script.
    rec = Recorder(sink, capture_values=capture, scope=Scope(roots=[WORKLOAD_DIR]))
    _gc_settled()
    with rec:
        t0 = time.perf_counter()
        work()
        elapsed = time.perf_counter() - t0
    return elapsed, len(sink.events)


def run_chrono_flow(work: Callable[[], Any]) -> tuple[float, int]:
    return _run_chrono(work, capture=False)


def run_chrono_capture(work: Callable[[], Any]) -> tuple[float, int]:
    return _run_chrono(work, capture=True)


RUNNERS: dict[str, Callable[[Callable[[], Any]], tuple[float, int]]] = {
    "baseline": run_baseline,
    "settrace_noop": run_settrace_noop,
    "pdb_unhit": run_pdb_unhit,
    "coverage_py": run_coverage_py,
    "chrono_flow": run_chrono_flow,
    "chrono_capture": run_chrono_capture,
}


# ---------------------------------------------------------------------------
# Child: one condition, one workload, one timed run.
# ---------------------------------------------------------------------------


def child_main(condition: str, workload_name: str) -> None:
    work = WORKLOADS[workload_name]
    work()  # warmup: imports, disk cache, cpu ramp -- uninstrumented, like a real run
    seconds, events = RUNNERS[condition](work)
    print(json.dumps({"seconds": seconds, "events": events}))


# ---------------------------------------------------------------------------
# Parent: orchestrate, aggregate, report.
# ---------------------------------------------------------------------------


def _p95(values: list[float]) -> float:
    """95th percentile. With small N this sits near the max; stated in the results."""
    if len(values) < 2:
        return values[0]
    return statistics.quantiles(values, n=20, method="inclusive")[18]


def measure(condition: str, workload: str, reps: int) -> dict[str, Any]:
    times: list[float] = []
    events = 0
    for _ in range(reps):
        proc = subprocess.run(  # noqa: S603 -- fixed argv, our own file, no shell
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
    }


def run_matrix(reps: int = 5, workloads: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """The whole matrix as structured rows -- the entry point `__main__` renders."""
    names = workloads if workloads is not None else tuple(WORKLOADS)
    rows: list[dict[str, Any]] = []
    for workload in names:
        base = None
        for condition in CONDITIONS:
            row = measure(condition, workload, reps)
            if condition == "baseline":
                base = row["median"]
            row["ratio"] = row["median"] / base if base else float("nan")
            rows.append(row)
            print(
                f"  {workload:<16}{condition:<18}"
                f"median={row['median'] * 1000:8.2f}ms  x{row['ratio']:<6.1f}"
                f"events={row['events'] if row['events'] >= 0 else '-':>10}",
                flush=True,
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Recording overhead vs. fair baselines.")
    parser.add_argument("--child", nargs=2, metavar=("CONDITION", "WORKLOAD"))
    parser.add_argument("--reps", type=int, default=5)
    args = parser.parse_args()

    if args.child:
        child_main(args.child[0], args.child[1])
        return 0

    run_matrix(reps=args.reps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
