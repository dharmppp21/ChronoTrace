"""Retroactive and conditional breakpoints, watchpoints, and index-backed reverse-continue.

The referee is `test_conditional_matches_a_live_pdb_oracle`: it runs the program under a
*real* conditional breakpoint (`sys.settrace`) and asserts ChronoTrace's retroactive query
finds exactly the same instants. That is the day's strong claim -- the breakpoint you set
afterwards finds what the breakpoint you set beforehand would have.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from chronotrace.query import QueryContext, RetroBreakpointQuery, WatchQuery
from chronotrace.query._resolve import resolve_file
from chronotrace.query.breakpoints import _capture_end
from chronotrace.reconstruct import Edge, continue_back

from .conftest import EXAMPLES, record_example


def _bp_line() -> int:
    """The line number of the `x = i * i` statement in loops.py (not the docstring's mention)."""
    for i, line in enumerate((EXAMPLES / "loops.py").read_text().splitlines(), 1):
        if line.strip().startswith("x = i * i"):
            return i
    raise AssertionError("could not find the breakpoint line in loops.py")


def _live_hits(entry: Callable[[], object], suffix: str, lineno: int, cond: str) -> list[Any]:
    """A live conditional breakpoint via `sys.settrace` -- the reference implementation.

    Returns the value of `i` at each instant the breakpoint would fire. This IS a real
    conditional breakpoint (it evaluates the condition in the running frame's locals), so it
    is the ground truth the retroactive query must match.
    """
    code = compile(cond, "<cond>", "eval")
    fired: list[Any] = []

    def trace(frame: Any, event: str, _arg: Any) -> Any:
        if (
            event == "line"
            and frame.f_code.co_filename.endswith(suffix)
            and frame.f_lineno == lineno
        ):
            with contextlib.suppress(Exception):  # a condition that raises did not fire, like pdb
                if eval(code, {"__builtins__": {}}, dict(frame.f_locals)):  # noqa: S307 -- the oracle
                    fired.append(frame.f_locals.get("i"))
        return trace

    old = sys.gettrace()
    sys.settrace(trace)
    try:
        entry()
    finally:
        sys.settrace(old)
    return fired


def _i_at(ctx: QueryContext, seq: int) -> Any:
    """`i`'s value at a breakpoint match -- reconstructed at the same instant the query used."""
    state = ctx.reconstructor.reconstruct(_capture_end(ctx.reader, seq))
    frame = state.frame(state.current_frame_id)
    assert frame is not None
    (i_id,) = ctx.db.execute("SELECT id FROM strings WHERE text = 'i'").fetchone()
    return ctx.resolver.resolve(frame.bindings[i_id])


def _matches(ctx: QueryContext, lineno: int, cond: str) -> list[Any]:
    """The `i` value at each *true* match of the conditional retroactive breakpoint."""
    result = RetroBreakpointQuery("loops.py", lineno, condition=cond).execute(ctx, limit=1000)
    return [_i_at(ctx, h.seq) for h in result.hits if "-> true" in (h.note or "")]


def test_retro_breakpoint_returns_every_hit_of_a_line(tmp_path: Path) -> None:
    """`loops.scan(20)` runs the breakpoint line twenty times -- an unconditional breakpoint."""
    path = record_example(tmp_path, "loops")
    with QueryContext.open(path) as ctx:
        result = RetroBreakpointQuery("loops.py", _bp_line()).execute(ctx, limit=100)
        assert len(result.hits) == 20


def test_conditional_matches_a_live_pdb_oracle(tmp_path: Path) -> None:
    """THE referee: retroactive conditional hits == a live conditional breakpoint's hits.

    For several conditions, the set of `i` values ChronoTrace matches after the fact must
    equal the set a real `sys.settrace` breakpoint fired on while the program ran.
    """
    if str(EXAMPLES) not in sys.path:
        sys.path.insert(0, str(EXAMPLES))
    import loops  # type: ignore[import-not-found]

    lineno = _bp_line()
    path = record_example(tmp_path, "loops")
    for cond in ("i > 15", "i % 2 == 0", "i < 3", "i == 7", "x > 200"):
        oracle = _live_hits(loops.main, "loops.py", lineno, cond)
        with QueryContext.open(path) as ctx:
            chrono = _matches(ctx, lineno, cond)
        assert chrono == oracle, f"condition {cond!r}: chrono {chrono} != oracle {oracle}"


def test_an_out_of_scope_condition_is_flagged_unknown_not_dropped(tmp_path: Path) -> None:
    """A name the frame never had cannot be evaluated -- every hit is flagged UNKNOWN, not False."""
    path = record_example(tmp_path, "loops")
    with QueryContext.open(path) as ctx:
        result = RetroBreakpointQuery(
            "loops.py", _bp_line(), condition="undefined_name > 0"
        ).execute(ctx, limit=100)
        assert result.hits, "unknowns are returned, never silently dropped"
        assert all("UNKNOWN" in (h.note or "") for h in result.hits)


def test_continue_back_returns_the_previous_hit_of_any_breakpoint(tmp_path: Path) -> None:
    """Index-backed reverse-continue: the greatest breakpoint hit before the instant asked."""
    path = record_example(tmp_path, "loops")
    lineno = _bp_line()
    with QueryContext.open(path) as ctx:
        file_id, _ = resolve_file(ctx.db, "loops.py")
        hits = [
            h.seq for h in RetroBreakpointQuery("loops.py", lineno).execute(ctx, limit=1000).hits
        ]
        assert continue_back(ctx.db, [(file_id, lineno)], hits[-1]) == max(
            s for s in hits if s < hits[-1]
        )
        assert continue_back(ctx.db, [(file_id, lineno)], hits[0]) is Edge.BEGINNING


def test_watch_reports_each_change_old_to_new(simple_ctx: QueryContext) -> None:
    """`total` in simple.py goes unset -> 0 -> 4; the watch shows each transition."""
    result = WatchQuery("total").execute(simple_ctx)
    previews = [h.value_preview for h in result.hits]
    assert previews[0] == "unset -> 0"
    assert previews[-1] == "0 -> 4"


def test_watch_changed_to_filters_by_the_new_value(simple_ctx: QueryContext) -> None:
    result = WatchQuery("total", changed_to=4).execute(simple_ctx)
    assert [h.value_preview for h in result.hits] == ["0 -> 4"]
