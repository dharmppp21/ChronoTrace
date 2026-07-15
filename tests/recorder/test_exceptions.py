"""Exception origin vs propagation -- the distinction day 29's flagship query needs."""

from __future__ import annotations

from chronotrace.recorder import Event, EventKind, Recorder

from .conftest import record_example
from .invariants import assert_every_frame_dies_once, assert_frame_lifecycles_are_well_formed


def _record_example(func_name: str) -> tuple[list[Event], Recorder]:
    return record_example("exceptions", func_name)


def _exc_names(events: list[Event], rec: Recorder, kind: EventKind) -> list[str]:
    return [
        rec._exc_types.resolve(e.exc_type_id)
        for e in events
        if e.kind is kind and e.exc_type_id is not None
    ]


def test_one_exception_across_three_frames_has_exactly_one_origin() -> None:
    """The finding that reshaped the model.

    CPython fires RAISE in every frame an exception crosses: `_innermost` raises,
    then RAISE fires again in `_middle` and again in `deep_raise` for the SAME
    ValueError. Day 4's model called all three "origins", which would have made
    "jump to where this came from" land on the frame the user is already reading.
    Exactly one RAISE must survive.
    """
    events, rec = _record_example("deep_raise")
    raises = [e for e in events if e.kind is EventKind.RAISE]
    assert len(raises) == 1, f"expected exactly one origin, got {len(raises)}"
    assert _exc_names(events, rec, EventKind.RAISE) == ["ValueError"]


def test_propagation_is_recorded_as_unwind_not_as_origin() -> None:
    """Nothing is lost by suppressing non-origin RAISEs.

    The frames the exception passed through are still visible -- as UNWIND, which
    says strictly more ("this frame exited because of it").
    """
    events, rec = _record_example("deep_raise")
    unwinds = [e for e in events if e.kind is EventKind.UNWIND]
    assert len(unwinds) == 2, "_innermost and _middle both unwound"
    assert _exc_names(events, rec, EventKind.UNWIND) == ["ValueError", "ValueError"]


def test_the_origin_precedes_every_unwind() -> None:
    """seq ordering makes 'walk back to the origin' a range scan, not a search."""
    events, _ = _record_example("deep_raise")
    origin = next(e for e in events if e.kind is EventKind.RAISE)
    unwinds = [e for e in events if e.kind is EventKind.UNWIND]
    assert all(u.seq > origin.seq for u in unwinds)


def test_handled_bounds_the_unwind() -> None:
    events, rec = _record_example("deep_raise")
    handled = [e for e in events if e.kind is EventKind.EXCEPTION_HANDLED]
    assert handled, "the exception was caught; that must be recorded"
    assert _exc_names(events, rec, EventKind.EXCEPTION_HANDLED) == ["ValueError"]
    last_unwind = max(e.seq for e in events if e.kind is EventKind.UNWIND)
    assert handled[0].seq > last_unwind


def test_raised_and_caught_in_one_frame_produces_no_unwind() -> None:
    """The shape a model watching only UNWIND would miss entirely."""
    events, rec = _record_example("handled_in_place")
    assert _exc_names(events, rec, EventKind.RAISE) == ["ZeroDivisionError"]
    assert [e for e in events if e.kind is EventKind.UNWIND] == []
    assert _exc_names(events, rec, EventKind.EXCEPTION_HANDLED) == ["ZeroDivisionError"]


def test_raise_from_records_both_exceptions_as_distinct_origins() -> None:
    """`raise ... from` -- two different exception objects, two origins.

    The KeyError is born, then the RuntimeError is born while handling it. Both are
    origins; neither is propagation of the other. The `__cause__` LINK between them
    is not recorded today -- see the module note in events.py and issue for day 29.
    """
    events, rec = _record_example("raise_from")
    assert _exc_names(events, rec, EventKind.RAISE) == ["KeyError", "RuntimeError"]


def test_implicit_context_also_yields_two_origins() -> None:
    """`__context__` chaining. Same event shape as `raise from`; the link differs."""
    events, rec = _record_example("implicit_context")
    assert _exc_names(events, rec, EventKind.RAISE) == ["KeyError", "RuntimeError"]


def test_exception_frames_still_balance() -> None:
    """UNWIND must pop. Not popping leaks a frame per exception, forever."""
    for shape in ("deep_raise", "raise_from", "implicit_context", "handled_in_place"):
        events, _ = _record_example(shape)
        assert_frame_lifecycles_are_well_formed(events)
        assert_every_frame_dies_once(events)


def test_a_frame_that_unwinds_never_also_returns() -> None:
    """Abnormal exit is a distinct kind, not a flavour of RETURN.

    Day 27 colours abnormal exits in the call tree; a frame reported as both would
    make that column meaningless.
    """
    events, _ = _record_example("deep_raise")
    returned = {e.frame_id for e in events if e.kind is EventKind.RETURN}
    unwound = {e.frame_id for e in events if e.kind is EventKind.UNWIND}
    assert returned & unwound == set()
