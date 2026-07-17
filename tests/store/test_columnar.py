"""Columnar encoding: prove it is lossless (including the None fields and dropped
seqs) and that it actually compresses -- a column stored raw would pass round-trip
but defeat the whole point, so the size is asserted too."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from chronotrace.recorder.events import Event, EventKind
from chronotrace.recorder.values import ValueRef
from chronotrace.store.columnar import decode_events, encode_events

_ints = st.integers(min_value=0, max_value=2**48)
_opt = st.one_of(st.none(), st.integers(min_value=0, max_value=2**31))
_events = st.lists(
    st.builds(
        Event,
        seq=_ints,
        kind=st.sampled_from(EventKind),
        timestamp_ns=_ints,
        thread_id=_ints,
        frame_id=_ints,
        code_id=_ints,
        lineno=st.integers(min_value=0, max_value=2**20),
        name_id=_opt,
        value_ref=_opt,
        exc_type_id=_opt,
    ),
    max_size=200,
)


@given(_events)
def test_round_trips_any_event_list(events: list[Event]) -> None:
    assert decode_events(encode_events(events)) == events


def test_empty_batch_round_trips() -> None:
    assert decode_events(encode_events([])) == []


def test_none_fields_survive() -> None:
    """LINE has no name/value/exc; VAR_WRITE has name+value; RAISE has exc."""
    events = [
        Event(
            seq=0, kind=EventKind.LINE, timestamp_ns=1, thread_id=1, frame_id=1, code_id=1, lineno=5
        ),
        Event(
            seq=1,
            kind=EventKind.VAR_WRITE,
            timestamp_ns=2,
            thread_id=1,
            frame_id=1,
            code_id=1,
            lineno=5,
            name_id=3,
            value_ref=ValueRef(7),
        ),
        Event(
            seq=2,
            kind=EventKind.RAISE,
            timestamp_ns=3,
            thread_id=1,
            frame_id=1,
            code_id=1,
            lineno=6,
            exc_type_id=2,
        ),
    ]
    assert decode_events(encode_events(events)) == events


def test_dropped_events_still_encode() -> None:
    """After a drop, seq is no longer +1. Delta stores the gap; round-trip holds."""
    events = [
        Event(seq=s, kind=EventKind.LINE, timestamp_ns=s, thread_id=1, frame_id=1, code_id=1)
        for s in (0, 1, 2, 900, 901, 5000)  # two gaps, as if events were dropped
    ]
    assert decode_events(encode_events(events)) == events


def test_realistic_stream_compresses_far_below_raw() -> None:
    """A tight loop: seq +1, one thread, one function. Must shrink, not just survive.

    Raw would be 10 columns x 8 bytes x N. If the encoded size is anywhere near
    that, the columns are not actually being delta/RLE-encoded -- the exact failure
    this test exists to catch.
    """
    n = 2000
    events = [
        Event(
            seq=s,
            kind=EventKind.LINE,
            timestamp_ns=1000 + s,
            thread_id=42,
            frame_id=1,
            code_id=1,
            lineno=10 + s % 5,
        )
        for s in range(n)
    ]
    encoded = encode_events(events)
    raw_size = n * 10 * 8
    assert len(encoded) < raw_size // 10, f"{len(encoded)} B is not << raw {raw_size} B"
    assert decode_events(encoded) == events
