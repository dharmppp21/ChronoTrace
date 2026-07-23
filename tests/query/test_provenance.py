"""Value provenance -- the exact write and the labelled heuristic -- plus last-write and calls.

The headline is the demo-bug test: a single provenance query lands on the culprit line of
`examples/buggy_pipeline.py`. The rest pin the honesty (the heuristic is labelled a
heuristic) and the two smaller causal queries that share the commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chronotrace.query import (
    CallersOfQuery,
    CallTreeQuery,
    LastWriteBeforeQuery,
    QueryContext,
    UnknownFunction,
    UnknownName,
    ValueProvenanceQuery,
    VarWritesQuery,
)

from .conftest import EXAMPLES, record_example

# -- provenance -------------------------------------------------------------------------


def test_provenance_names_the_exact_producing_write(simple_ctx: QueryContext) -> None:
    """`result = n * 2` in `double`: the first hit is that write, marked as the exact answer."""
    writes = VarWritesQuery("result").execute(simple_ctx).hits
    res = ValueProvenanceQuery("result", writes[0].seq + 1).execute(simple_ctx)
    assert res.hits[0].seq == writes[0].seq
    assert res.hits[0].function == "double"
    assert "the write that set 'result'" in (res.hits[0].note or "")


def test_provenance_offers_likely_inputs_clearly_labelled(simple_ctx: QueryContext) -> None:
    """`result = n * 2` reads `n`; the heuristic surfaces `n`'s write, and says it is a guess."""
    writes = VarWritesQuery("result").execute(simple_ctx).hits
    res = ValueProvenanceQuery("result", writes[0].seq + 1).execute(simple_ctx)
    assert "HEURISTIC" in (res.hits[0].note or ""), "the caveat must be unmissable"
    inputs = [h for h in res.hits[1:] if "likely input" in (h.note or "")]
    assert any("'n'" in (h.note or "") for h in inputs), "n is read on the line"


def test_provenance_of_an_unknown_name_is_a_typo(simple_ctx: QueryContext) -> None:
    with pytest.raises(UnknownName):
        ValueProvenanceQuery("no_such_var", 5).execute(simple_ctx)


def test_provenance_finds_the_culprit_in_the_demo_bug(tmp_path: Path) -> None:
    """THE demo: one query lands on `dict.fromkeys`, the shared-dict line that is the bug.

    `totals` is initialised once by the aliasing `dict.fromkeys(...)`; every later "write" to
    it is that one shared dict mutating. Provenance of `totals`, taken just after its first
    write, points exactly at the culprit line -- the mistake that happened ~360 events before
    any wrong number was printed.
    """
    culprit_line = next(
        i
        for i, line in enumerate((EXAMPLES / "buggy_pipeline.py").read_text().splitlines(), 1)
        if "= dict.fromkeys" in line  # the code line, not the docstring's mention of it
    )
    path = record_example(tmp_path, "buggy_pipeline")
    with QueryContext.open(path) as ctx:
        first_write = VarWritesQuery("totals").execute(ctx).hits[0].seq
        res = ValueProvenanceQuery("totals", first_write + 1).execute(ctx)
        assert res.hits, "provenance must find the producing write"
        assert res.hits[0].seq == first_write
        # the value appears one line after the assignment (locals captured per line), so the
        # write lands on the fromkeys line or the one just after -- the culprit init region,
        # far from where the wrong number prints (~360 events later).
        assert res.hits[0].lineno in (culprit_line, culprit_line + 1)
        assert res.hits[0].file is not None and res.hits[0].file.endswith("buggy_pipeline.py")


# -- last write -------------------------------------------------------------------------


def test_last_write_before_returns_the_single_most_recent(simple_ctx: QueryContext) -> None:
    """The primitive: one hit, the write immediately before the instant asked about."""
    writes = VarWritesQuery("total").execute(simple_ctx).hits
    assert len(writes) >= 2, "total is written more than once in simple.py"
    res = LastWriteBeforeQuery("total", writes[-1].seq).execute(simple_ctx)
    assert len(res.hits) == 1
    assert res.hits[0].seq == writes[-2].seq, "the one strictly before the last"


def test_last_write_before_the_first_write_is_empty(simple_ctx: QueryContext) -> None:
    writes = VarWritesQuery("total").execute(simple_ctx).hits
    assert LastWriteBeforeQuery("total", writes[0].seq).execute(simple_ctx).hits == ()


# -- callers and call tree --------------------------------------------------------------


def test_callers_of_finds_every_invocation(simple_ctx: QueryContext) -> None:
    """`double` is called four times in `simple.py`, each from `quadruple`."""
    res = CallersOfQuery("double").execute(simple_ctx)
    assert len(res.hits) == 4
    assert all(h.function == "double" for h in res.hits)
    assert all("quadruple" in (h.note or "") for h in res.hits)


def test_callers_of_an_unknown_function_is_a_typo(simple_ctx: QueryContext) -> None:
    with pytest.raises(UnknownFunction):
        CallersOfQuery("no_such_function").execute(simple_ctx)


def test_call_tree_lists_a_frames_direct_children(simple_ctx: QueryContext) -> None:
    """`main` calls `quadruple` twice; those two are its direct children, in call order."""
    (main_frame,) = simple_ctx.db.execute(
        "SELECT fr.frame_id FROM frames fr JOIN codes c ON fr.code_id = c.code_id "
        "WHERE c.qualname = 'main'"
    ).fetchone()
    res = CallTreeQuery(int(main_frame)).execute(simple_ctx)
    assert [h.function for h in res.hits] == ["quadruple", "quadruple"]
