"""Exception origins and journeys -- day 29's raw material, checked today.

The claim that matters: `is_origin` marks where an exception was *born*, exactly once,
even though it crosses several frames. Day 6 made that possible by suppressing the
non-origin RAISE events CPython re-fires; this verifies the index preserved it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chronotrace.index import of_type, origin_of, propagation_of
from chronotrace.recorder.events import EventKind

from .conftest import Indexed, index_example


@pytest.fixture
def raised(tmp_path: Path) -> Indexed:
    """`examples/exceptions.py` -- raises, propagates across frames, and handles."""
    return index_example(tmp_path, "exceptions")


def test_every_exception_event_is_indexed(raised: Indexed) -> None:
    kinds = {EventKind.RAISE, EventKind.UNWIND, EventKind.EXCEPTION_HANDLED}
    expected = sorted(e.seq for e in raised.events if e.kind in kinds and e.exc_type_id is not None)
    actual = sorted(seq for (seq,) in raised.db.execute("SELECT seq FROM exceptions"))
    assert expected, "the fixture must raise something"
    assert actual == expected


def test_an_origin_is_marked_exactly_where_the_raise_was(raised: Indexed) -> None:
    """CPython re-fires RAISE in every frame an exception crosses; day 6 suppressed the
    repeats so "where did this come from?" has one answer instead of three."""
    origins = {seq for seq, o in raised.db.execute("SELECT seq, is_origin FROM exceptions") if o}
    assert origins == {e.seq for e in raised.events if e.kind is EventKind.RAISE}


def test_a_propagation_points_back_to_its_origin(raised: Indexed) -> None:
    """The day-29 query: standing at a traceback frame, jump to where it was born."""
    rows = raised.db.execute("SELECT seq, is_origin, cause_seq FROM exceptions ORDER BY seq")
    non_origins = [(seq, cause) for seq, origin, cause in rows if not origin]
    assert non_origins, "the fixture must propagate across at least one frame"
    for seq, cause in non_origins:
        assert cause is not None, f"seq {seq} has no origin recorded"
        found = origin_of(raised.db, seq)
        assert found is not None and found[0] == cause
        assert found[0] <= seq, "an exception cannot originate after it propagates"


def test_the_journey_is_ordered_and_starts_at_the_origin(raised: Indexed) -> None:
    for (origin_seq,) in raised.db.execute("SELECT seq FROM exceptions WHERE is_origin=1"):
        journey = propagation_of(raised.db, origin_seq)
        assert journey, "an origin must at least include itself"
        assert journey[0][0] == origin_seq
        seqs = [seq for seq, _f, _o in journey]
        assert seqs == sorted(seqs)


def test_lookups_by_type_use_the_index(raised: Indexed) -> None:
    type_id = raised.db.execute("SELECT type_id FROM exceptions LIMIT 1").fetchone()[0]
    assert of_type(raised.db, type_id)
    plan = " ".join(
        str(row[-1])
        for row in raised.db.execute(
            "EXPLAIN QUERY PLAN SELECT seq FROM exceptions WHERE type_id=? ORDER BY seq",
            (type_id,),
        )
    )
    assert "SCAN" not in plan.upper(), plan


def test_exception_type_names_are_resolvable(raised: Indexed) -> None:
    """A `type_id` is meaningless without `exc_types` -- the point of persisting them."""
    assert raised.exc_types, "exc_types must be populated from the recording"
    type_id = raised.db.execute("SELECT type_id FROM exceptions LIMIT 1").fetchone()[0]
    assert type_id in set(raised.exc_types.values())


def test_a_program_that_raises_nothing_has_an_empty_table(simple: Indexed) -> None:
    """An empty table, not a missing one: every query must work on a clean recording."""
    assert simple.db.execute("SELECT count(*) FROM exceptions").fetchone()[0] == 0
    assert of_type(simple.db, 0) == []
    assert origin_of(simple.db, 0) is None
