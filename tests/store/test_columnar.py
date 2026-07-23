"""Columnar encoding: prove it is lossless (including the None fields and dropped
seqs) and that it actually compresses -- a column stored raw would pass round-trip
but defeat the whole point, so the size is asserted too. Also proves the format-1.7
self-describing column count: a pre-1.7 payload decodes with the new fields as None."""

from __future__ import annotations

import struct

from hypothesis import given
from hypothesis import strategies as st

from chronotrace.recorder.events import Event, EventKind
from chronotrace.recorder.values import ValueRef
from chronotrace.store.columnar import (
    LEGACY_NCOLS,
    decode_events,
    encode_events,
    pack_columns,
)
from chronotrace.store.constants import FORMAT_VERSION_MINOR

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
        exc_cause_seq=_opt,
        exc_context_seq=_opt,
    ),
    max_size=200,
)


def _round_trip(events: list[Event]) -> list[Event]:
    return decode_events(encode_events(events), FORMAT_VERSION_MINOR)


@given(_events)
def test_round_trips_any_event_list(events: list[Event]) -> None:
    assert _round_trip(events) == events


def test_empty_batch_round_trips() -> None:
    assert _round_trip([]) == []


def test_none_fields_survive() -> None:
    """LINE has no name/value/exc; VAR_WRITE has name+value; a chained RAISE has the links."""
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
            exc_cause_seq=0,
            exc_context_seq=1,
        ),
    ]
    assert _round_trip(events) == events


def test_dropped_events_still_encode() -> None:
    """After a drop, seq is no longer +1. Delta stores the gap; round-trip holds."""
    events = [
        Event(seq=s, kind=EventKind.LINE, timestamp_ns=s, thread_id=1, frame_id=1, code_id=1)
        for s in (0, 1, 2, 900, 901, 5000)  # two gaps, as if events were dropped
    ]
    assert _round_trip(events) == events


def test_a_pre_1_7_payload_decodes_with_the_new_fields_as_none() -> None:
    """The backward-compatibility contract: a 10-column payload (no `ncols`) still reads.

    Crafted as a 1.6 writer would: `u32 count` then exactly `LEGACY_NCOLS` columns, no
    self-describing prefix. A current reader, told `minor < 7`, must decode those ten fields
    and leave `exc_cause_seq`/`exc_context_seq` None rather than misparse the payload.
    """
    events = [
        Event(
            seq=0,
            kind=EventKind.RAISE,
            timestamp_ns=1,
            thread_id=1,
            frame_id=1,
            code_id=1,
            lineno=9,
            exc_type_id=4,
        ),
    ]
    columns = [
        [0],
        [int(EventKind.RAISE)],
        [1],
        [1],
        [1],
        [1],
        [9],
        [-1],
        [-1],
        [4],
    ]  # the ten legacy fields, in order, None -> -1
    legacy_payload = struct.pack("<I", len(events)) + pack_columns(columns)
    assert len(columns) == LEGACY_NCOLS
    decoded = decode_events(legacy_payload, 6)
    assert decoded == events
    assert decoded[0].exc_cause_seq is None
    assert decoded[0].exc_context_seq is None


def test_realistic_stream_compresses_far_below_raw() -> None:
    """A tight loop: seq +1, one thread, one function. Must shrink, not just survive.

    Raw would be twelve columns x 8 bytes x N. If the encoded size is anywhere near
    that, the columns are not actually being delta/RLE-encoded -- the exact failure
    this test exists to catch. The two all-None exception-chain columns cost nothing.
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
    raw_size = n * 12 * 8
    assert len(encoded) < raw_size // 10, f"{len(encoded)} B is not << raw {raw_size} B"
    assert decode_events(encoded, FORMAT_VERSION_MINOR) == events
