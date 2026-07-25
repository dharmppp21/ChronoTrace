"""The day-31 dogfooding session, frozen into tests.

Three bugs debugged with queries alone (no prints, no re-runs, no reading the code) -- each
one's *winning query* is asserted here, so a future refactor that quietly breaks the demo
breaks a test instead. This is the point of dogfooding: the session becomes a permanent
guarantee that the engine still finds the bugs it was shown to find.
"""

from __future__ import annotations

from pathlib import Path

from chronotrace.query import QueryContext, VarWritesQuery, WatchQuery

from .conftest import record_example


def test_off_by_one_loop_variable_stops_short(tmp_path: Path) -> None:
    """`average` sums `nums[0..len-2]`: the loop variable never reaches the last index.

    Found by `--var-writes i`: `i` takes 0, 1, 2 and stops -- index 3 (value 40) is never
    added, so `total` reaches 60 instead of 100.
    """
    path = record_example(tmp_path, "mystery.off_by_one")
    with QueryContext.open(path) as ctx:
        i_values = [h.value_preview for h in VarWritesQuery("i").execute(ctx).hits]
        assert i_values == ["0", "1", "2"], "i must stop at 2; the last element is never summed"


def test_sticky_default_bucket_is_not_empty_on_the_second_call(tmp_path: Path) -> None:
    """The mutable default persists: `collect`'s `bucket` starts as `['first']` the second time.

    Found by `--watch bucket`: the second call's opening value is `['first']`, not `[]` -- the
    two calls share one list.
    """
    path = record_example(tmp_path, "mystery.sticky_default")
    with QueryContext.open(path) as ctx:
        previews = [h.value_preview for h in WatchQuery("bucket").execute(ctx).hits]
        assert "['first'] -> ['first']" in previews, "the second call inherited the first's list"


def test_late_binding_lambda_reads_the_final_loop_value(tmp_path: Path) -> None:
    """The closure captures the loop variable: the lambda reads `factor = 4`, not 3.

    Found by `--var-writes factor`: the write inside the lambda frame is 4 (the loop's final
    value), so the "multiply by 3" function actually multiplies by 4.
    """
    path = record_example(tmp_path, "mystery.late_binding")
    with QueryContext.open(path) as ctx:
        writes = VarWritesQuery("factor").execute(ctx).hits
        in_lambda = [h for h in writes if h.function and "lambda" in h.function]
        assert in_lambda, "factor must be read inside the lambda frame"
        assert in_lambda[-1].value_preview == "4", "the lambda sees the final loop value, not 3"
