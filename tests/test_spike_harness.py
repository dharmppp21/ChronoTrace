"""Guards the overhead spike against measuring nothing.

A benchmark whose callback never fires reports beautiful numbers and is entirely
worthless. That failure is silent -- the harness runs, prints a table, and lies.
Since day 3's ADR-0001 bets the architecture on this spike's output, the harness
gets a test even though the spike code itself is throwaway.

These drive the harness through its subprocess CLI rather than importing it.
That is deliberate twice over: it tests the contract the harness actually runs
under, and it keeps ``spikes/`` off the import graph of anything under
``mypy --strict``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).parent.parent / "spikes" / "bench_overhead.py"


def _child(condition: str, workload: str) -> dict[str, float]:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(BENCH), "--child", condition, workload],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    result: dict[str, float] = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


def test_bench_script_exists() -> None:
    assert BENCH.is_file(), f"spike harness missing at {BENCH}"


def test_baseline_reports_no_events() -> None:
    """Baseline must be genuinely uninstrumented, or every ratio is wrong."""
    assert _child("baseline", "fib_recursive")["events"] == 0


@pytest.mark.parametrize(
    "condition", ["settrace_noop", "mon_line_noop", "mon_line_append", "mon_line_scoped"]
)
def test_instrumented_conditions_actually_fire(condition: str) -> None:
    """A callback that never fires is the classic way to fool yourself.

    Asserted rather than assumed: if a condition reports zero events, its
    overhead number is measuring an interpreter that was never instrumented.
    """
    assert _child(condition, "fib_recursive")["events"] > 0


def test_instrumentation_costs_more_than_baseline() -> None:
    """Sanity: observing every line cannot be free.

    Uses fib_recursive (call- and line-heavy) so the signal is far outside
    scheduler noise. A 2x floor rather than a bare '>' keeps this from flaking
    on a noisy CI runner while still failing hard if instrumentation silently
    stopped happening.
    """
    base = _child("baseline", "fib_recursive")["seconds"]
    traced = _child("mon_line_append", "fib_recursive")["seconds"]
    assert traced > base * 2, f"instrumented={traced:.4f}s vs baseline={base:.4f}s"


def test_scoping_does_not_lose_in_scope_events() -> None:
    """DISABLE must only silence out-of-scope code, never the code under study.

    If scoping ever dropped in-scope lines it would look like a huge performance
    win and be a correctness disaster -- exactly the kind of trade that must
    never be made silently.
    """
    scoped = _child("mon_line_scoped", "json_pipeline")["events"]
    unscoped = _child("mon_line_append", "json_pipeline")["events"]
    assert 0 < scoped <= unscoped
