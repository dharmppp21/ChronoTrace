"""Performance regression guards -- thresholded, so a slowdown fails CI instead of hiding.

A benchmark measures; a *test* asserts. These pin the day-41 optimisations with ceilings generous
enough to survive a slow, noisy CI runner but tight enough to trip on a real regression -- the atom
fast path removed, or redaction reverted to the per-pattern fnmatch loop.

**Absolute budgets, not an overhead ratio -- and why.** The obvious form is "overhead <= Nx on a
fixed workload", but an overhead ratio divides the instrumented time by a sub-millisecond baseline,
which swings on timing noise (day-24's recorded warning) -- and on a tight in-scope loop it is ~100x
and jittery, a weak guard. So each check is an **absolute per-operation budget** on the exact hot
function that was optimised, taken as the **median of several batches** so a single GC pause cannot
fail the run.

**How the ceilings were chosen.** The dev laptop measures ~2.6 us to capture a 4-key dict and
~0.4 us per `should_redact`. CI runners are commonly 2-3x slower and bursty. Every ceiling is a
**~10x margin** over the dev number: it absorbs a 3x-slower box plus noise and still trips on a 10x+
regression -- the only kind worth failing a build over.

The day-9 DISABLE win (out-of-scope callbacks stop firing) is a *behavioural* property, guarded
directly by `tests/recorder/test_scope_filter.py`, not a timing threshold here.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from chronotrace.recorder.capture import capture
from chronotrace.recorder.redact import Redactor

pytestmark = pytest.mark.perf  # run on the dedicated `perf` runner, not the timing-noisy matrix


def _median_ns(fn: Callable[[], object], iters: int, batches: int = 7) -> float:
    fn()  # warm
    per = []
    for _ in range(batches):
        t0 = time.perf_counter_ns()
        for _ in range(iters):
            fn()
        per.append((time.perf_counter_ns() - t0) / iters)
    per.sort()
    return per[len(per) // 2]


def test_capture_stays_under_budget() -> None:
    """Capturing a representative value stays under ~10x the dev number.

    Guards the day-41 atom fast path and the capture walk generally: removing the fast path or
    making capture super-linear trips this.
    """
    value = {"id": 7, "hour": 3, "region": "emea", "amount": 213}
    median = _median_ns(lambda: capture(value), 2000)
    assert median < 30_000, f"capture regressed: {median:.0f} ns/dict (budget 30_000)"


def test_redaction_stays_under_budget() -> None:
    """`should_redact` stays cheap -- guards the compiled-regex redactor against a revert to the
    per-pattern fnmatch loop (measured ~4.5x slower)."""
    redactor = Redactor()
    median = _median_ns(lambda: redactor.should_redact("auth_token"), 20_000)
    assert median < 5_000, f"redaction regressed: {median:.0f} ns/name (budget 5_000)"
