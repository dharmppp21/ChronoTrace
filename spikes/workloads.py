"""Four workloads that represent real programs rather than microbenchmark lies.

Each is deterministic (no randomness, no network, no wall-clock dependence) so
that repeated runs measure the interpreter and not the weather. Sizes are tuned
so an uninstrumented run lands around 30-100ms: long enough that timer
resolution is irrelevant, short enough that a 60x instrumented run is still a
few seconds.

Why these four
--------------
* ``tight_loop``    -- worst case. Maximum Python lines per second, nothing else.
                       If overhead is tolerable here it is tolerable anywhere,
                       and this is the number a hostile reader will quote.
* ``fib_recursive`` -- call-heavy. Exercises PY_START/PY_RETURN rather than LINE,
                       which is a different cost centre in the interpreter.
* ``json_pipeline`` -- realistic. This is the shape of code people actually debug:
                       parse, transform, aggregate.
* ``io_bound``      -- the control. Instrumentation cost is proportional to
                       *Python lines executed*, not to wall time. A program that
                       spends its life blocked executes almost no bytecode, so
                       tracing it is nearly free. This workload exists to prove
                       that claim rather than assert it.

The one non-obvious design choice
---------------------------------
``json_pipeline`` deliberately calls **pure-Python** standard library code
(``datetime.strptime`` -> ``_strptime``, ``statistics``, ``collections.Counter``)
and not only C-accelerated code.

This matters for the DISABLE experiment specifically. ``json.loads`` runs almost
entirely in the C scanner, which emits **no LINE events at all**. A workload built
only from C-accelerated calls would have nothing out-of-scope to disable, and the
scoping measurement would come back as "saves nothing" -- a true statement about
that workload and a completely misleading one about real programs. Real programs
spend real time in Python-level library code. The workload has to as well, or the
number we are about to bet the architecture on is a lie.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# 1. Tight numeric loop -- the worst case
# ---------------------------------------------------------------------------


def tight_loop(n: int = 150_000) -> int:
    """Sum a cheap arithmetic series. ~5 Python lines per iteration.

    Args:
        n: iteration count.

    Returns:
        An accumulator, returned only so the loop cannot be optimised away.

    Complexity: O(n) time, O(1) space.
    """
    total = 0
    for i in range(n):
        x = i * 3
        y = x % 7
        if y > 3:
            total += y
        else:
            total -= 1
    return total


# ---------------------------------------------------------------------------
# 2. Recursion -- call-heavy
# ---------------------------------------------------------------------------


def fib_recursive(n: int = 24) -> int:
    """Naive Fibonacci. Deliberately exponential: we want the call traffic.

    Args:
        n: Fibonacci index.

    Returns:
        The nth Fibonacci number.

    Complexity: O(phi**n) calls -- that is the point, not a defect.
    """
    if n < 2:
        return n
    return fib_recursive(n - 1) + fib_recursive(n - 2)


# ---------------------------------------------------------------------------
# 3. Realistic mixed pipeline -- what people actually debug
# ---------------------------------------------------------------------------

_RECORD_COUNT = 1200

# Built once at import so the timed region measures the pipeline, not setup.
_RAW_JSON: str = json.dumps(
    [
        {
            "id": i,
            "ts": f"2026-07-{(i % 28) + 1:02d} {(i % 24):02d}:{(i % 60):02d}:00",
            "region": ["emea", "apac", "amer"][i % 3],
            "amount": (i * 37) % 500,
        }
        for i in range(_RECORD_COUNT)
    ]
)


def json_pipeline() -> dict[str, Any]:
    """Parse JSON, normalise timestamps, aggregate by region.

    Deliberately routes through pure-Python stdlib (``strptime``, ``statistics``)
    so that out-of-scope LINE events exist for the DISABLE experiment to act on.
    See this module's docstring.

    Returns:
        Per-region totals and the mean amount.

    Complexity: O(n) in record count, dominated by strptime.
    """
    records: list[dict[str, Any]] = json.loads(_RAW_JSON)

    parsed: list[dict[str, Any]] = []
    for rec in records:
        when = datetime.strptime(rec["ts"], "%Y-%m-%d %H:%M:%S")
        parsed.append(
            {
                "id": rec["id"],
                "hour": when.hour,
                "region": rec["region"],
                "amount": rec["amount"],
            }
        )

    by_region: Counter[str] = Counter()
    for rec in parsed:
        by_region[rec["region"]] += rec["amount"]

    amounts = [r["amount"] for r in parsed]
    return {
        "by_region": dict(by_region),
        "mean": statistics.mean(amounts),
        "n": len(parsed),
    }


# ---------------------------------------------------------------------------
# 4. I/O-bound -- the control
# ---------------------------------------------------------------------------


def io_bound(iterations: int = 150, nap: float = 0.0006) -> int:
    """Spend wall time blocked while executing almost no bytecode.

    ``time.sleep`` rather than real disk I/O: disk caching and scheduler noise
    add variance that would swamp the signal we are trying to measure, and the
    property under test is not "disks are slow" -- it is that instrumentation
    cost tracks *lines executed*, not elapsed time. Sleep isolates that cleanly.

    Args:
        iterations: number of naps.
        nap: seconds per nap.

    Returns:
        The iteration count, so the loop is not optimised away.

    Complexity: O(iterations) blocked time, O(iterations) Python lines.
    """
    count = 0
    for _ in range(iterations):
        time.sleep(nap)
        count += 1
    return count


WORKLOADS = {
    "tight_loop": tight_loop,
    "fib_recursive": fib_recursive,
    "json_pipeline": json_pipeline,
    "io_bound": io_bound,
}
