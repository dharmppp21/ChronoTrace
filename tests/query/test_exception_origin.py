"""Exception origin and cause chains -- the query no Python traceback can answer.

Golden results for every raise shape in `examples/exceptions.py`: the origin is exactly
right, and a chain (two-deep or five-deep) walks to its true root. These are facts about the
recorder + index + query together, on real recordings, not mocks.
"""

from __future__ import annotations

from pathlib import Path

from chronotrace.query import ExceptionOriginQuery, QueryContext

from .conftest import record_example


def _origin_seq(ctx: QueryContext, exc_type: str) -> int:
    """The origin RAISE seq of the (single) recorded exception of `exc_type`."""
    row = ctx.db.execute(
        "SELECT e.seq FROM exceptions e JOIN exc_types t ON e.type_id = t.id "
        "WHERE t.text = ? AND e.is_origin = 1 ORDER BY e.seq",
        (exc_type,),
    ).fetchone()
    assert row is not None, f"no recorded origin for {exc_type}"
    return int(row[0])


def test_deep_raise_origin_is_where_it_was_born(tmp_path: Path) -> None:
    """`_innermost` raises; `sys.monitoring` re-fires RAISE up the stack, but the origin is one.

    The query lands on `_innermost`, where the locals that caused the ValueError still live --
    not on `_middle` or `deep_raise`, the frames a traceback would show it crossing.
    """
    path = record_example(tmp_path, "exceptions", "deep_raise")
    with QueryContext.open(path) as ctx:
        res = ExceptionOriginQuery(_origin_seq(ctx, "ValueError")).execute(ctx)
        assert len(res.hits) == 1
        assert res.hits[0].value_preview == "ValueError"
        assert res.hits[0].function == "_innermost"


def test_raise_from_walks_to_the_key_error_root(tmp_path: Path) -> None:
    """`raise RuntimeError from KeyError`: the chain is RuntimeError -> KeyError (the root)."""
    path = record_example(tmp_path, "exceptions", "raise_from")
    with QueryContext.open(path) as ctx:
        res = ExceptionOriginQuery(_origin_seq(ctx, "RuntimeError")).execute(ctx)
        assert [h.value_preview for h in res.hits] == ["RuntimeError", "KeyError"]
        assert "direct cause" in (res.hits[1].note or "")
        assert "root cause" in (res.hits[1].note or "")


def test_implicit_context_walks_via_context(tmp_path: Path) -> None:
    """No explicit `from`, so the chain is followed through `__context__`, not `__cause__`.

    This is the case the in-flight stack could not recover (the KeyError is handled before the
    RuntimeError raises), so it proves the recorded object link is what makes the walk work.
    """
    path = record_example(tmp_path, "exceptions", "implicit_context")
    with QueryContext.open(path) as ctx:
        res = ExceptionOriginQuery(_origin_seq(ctx, "RuntimeError")).execute(ctx)
        assert [h.value_preview for h in res.hits] == ["RuntimeError", "KeyError"]
        assert "__context__" in (res.hits[1].note or "")


def test_a_five_deep_chain_resolves_to_the_true_root(tmp_path: Path) -> None:
    """The buried root: RuntimeError <- TypeError <- IndexError <- KeyError <- ValueError.

    The walk must iterate all the way down, not stop at the first link -- the ValueError is
    the root, four hops from the surface.
    """
    path = record_example(tmp_path, "exceptions", "deep_chain")
    with QueryContext.open(path) as ctx:
        res = ExceptionOriginQuery(_origin_seq(ctx, "RuntimeError")).execute(ctx)
        assert [h.value_preview for h in res.hits] == [
            "RuntimeError",
            "TypeError",
            "IndexError",
            "KeyError",
            "ValueError",
        ]
        assert "root cause" in (res.hits[-1].note or "")


def test_a_non_exception_instant_yields_an_empty_result(tmp_path: Path) -> None:
    """Asked about an instant with no exception, the query says nothing -- it does not guess.

    seq 0 is the first CALL, not an exception; a recording where the exception was raised in
    unrecorded code reaches this same empty answer.
    """
    path = record_example(tmp_path, "exceptions", "deep_raise")
    with QueryContext.open(path) as ctx:
        assert ExceptionOriginQuery(0).execute(ctx).hits == ()
