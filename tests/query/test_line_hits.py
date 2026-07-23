"""Retroactive breakpoints as a query, and the three file/line situations kept distinct."""

from __future__ import annotations

import pytest

from chronotrace.query import LineHitsQuery, QueryContext, UnknownFile

from .conftest import fake_ctx, synthetic_db


def test_a_line_in_a_hot_function_returns_every_instant_it_ran(simple_ctx: QueryContext) -> None:
    """`simple.py:18` is `result = n * 2` inside `double`, which `main` calls four times."""
    result = LineHitsQuery("simple.py", 18).execute(simple_ctx)
    assert len(result.hits) == 4
    assert all(h.file is not None and h.file.endswith("simple.py") for h in result.hits)
    assert all(h.lineno == 18 for h in result.hits)
    assert result.hits == tuple(sorted(result.hits, key=lambda h: h.seq)), "in seq order"


def test_a_file_not_in_the_recording_is_a_typo_not_an_empty_result(
    simple_ctx: QueryContext,
) -> None:
    """The wrong path is a different answer from a line that never ran -- it raises."""
    with pytest.raises(UnknownFile):
        LineHitsQuery("does_not_exist.py", 1).execute(simple_ctx)


def test_a_line_that_never_executed_is_empty(simple_ctx: QueryContext) -> None:
    """Line 20 of `simple.py` is blank -- no bytecode, so no LINE event can ever land there.

    A robustly non-executable line, unlike a body line that merely did not run this time:
    the recording is present and the file is known, the line simply has nothing to execute.
    """
    result = LineHitsQuery("simple.py", 20).execute(simple_ctx)
    assert result.hits == ()
    assert result.next_cursor is None


def test_an_ambiguous_bare_filename_is_rejected_with_its_candidates() -> None:
    """Two recorded files named `app.py`: guessing one would answer about the wrong file."""
    db = synthetic_db()
    db.executemany(
        "INSERT INTO files(file_id, path) VALUES (?,?)", [(1, "/a/app.py"), (2, "/b/app.py")]
    )
    with pytest.raises(UnknownFile, match="ambiguous"):
        LineHitsQuery("app.py", 1).execute(fake_ctx(db))


def test_a_full_path_resolves_even_when_the_basename_is_ambiguous() -> None:
    """Naming the full path is how a user disambiguates -- it must beat the basename match."""
    db = synthetic_db()
    db.executemany(
        "INSERT INTO files(file_id, path) VALUES (?,?)", [(1, "/a/app.py"), (2, "/b/app.py")]
    )
    db.execute("INSERT INTO line_hits(file_id, lineno, seq) VALUES (1, 5, 7)")
    result = LineHitsQuery("/a/app.py", 5).execute(fake_ctx(db))
    assert [h.seq for h in result.hits] == [7]
