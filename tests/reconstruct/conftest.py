"""A synthetic recording that exercises every shape reconstruction must handle.

Synthetic rather than a real recording on purpose: it is fast (the oracle is O(seq), so
the differential test runs it hundreds of times), deterministic, and lets the awkward
cases -- a suspended generator, an exception raised then handled, deeply nested calls --
be placed *deliberately* instead of hoped for.
"""

from __future__ import annotations

import io

import pytest

from chronotrace.recorder.capture import capture
from chronotrace.recorder.events import Event, EventKind
from chronotrace.recorder.values import ValueRef
from chronotrace.store import ChronoReader
from chronotrace.store.writer import ChronoWriter

K = EventKind
POOL_SIZE = 50
BLOCK_EVENTS = 512
KEYFRAME_INTERVAL = 200


def build_events(target: int) -> list[Event]:
    """An event stream with calls, nested frames, generators and exceptions."""
    events: list[Event] = []
    counter = {"seq": 0, "frame": 1}

    def emit(kind: EventKind, fid: int, line: int = 0, code: int = 1, **kw: int | None) -> None:
        seq = counter["seq"]
        ref = kw.get("ref")
        events.append(
            Event(
                seq=seq,
                kind=kind,
                timestamp_ns=1000 + seq,
                thread_id=1,
                frame_id=fid,
                code_id=code,
                lineno=line,
                name_id=kw.get("name"),
                value_ref=None if ref is None else ValueRef(ref),
                exc_type_id=kw.get("exc"),
            )
        )
        counter["seq"] = seq + 1

    def new_frame() -> int:
        counter["frame"] += 1
        return counter["frame"]

    main = new_frame()
    emit(K.CALL, main, line=1)
    while counter["seq"] < target:
        helper = new_frame()
        emit(K.CALL, helper, line=10, code=2)
        for i in range(3):
            emit(K.LINE, helper, line=11 + i)
            emit(K.VAR_WRITE, helper, line=11 + i, name=100 + i, ref=counter["seq"] % POOL_SIZE)
        if counter["seq"] % 97 < 4:  # a generator: yields (live but suspended), then resumes
            gen = new_frame()
            emit(K.CALL, gen, line=20, code=3)
            emit(K.VAR_WRITE, gen, line=21, name=200, ref=counter["seq"] % POOL_SIZE)
            emit(K.YIELD, gen, line=21)
            emit(K.LINE, helper, line=14)
            emit(K.RESUME, gen, line=21)
            emit(K.RETURN, gen, line=22)
        if counter["seq"] % 131 < 3:  # an exception raised and handled
            emit(K.RAISE, helper, line=15, exc=7)
            emit(K.EXCEPTION_HANDLED, helper, line=16, exc=7)
        emit(K.RETURN, helper, line=16)
        emit(K.LINE, main, line=2)
    return events


def write_recording(events: list[Event], **kw: int) -> bytes:
    """Write `events` (plus a pool of POOL_SIZE values) to a `.chrono` buffer."""
    buf = io.BytesIO()
    writer = ChronoWriter(
        buf,
        block_events=kw.get("block_events", BLOCK_EVENTS),
        keyframe_interval=kw.get("keyframe_interval", KEYFRAME_INTERVAL),
    )
    for i in range(POOL_SIZE):
        writer.add_value(capture({"v": i, "tag": f"value-{i}"}))
    for event in events:
        writer.add(event)
    writer.close()
    return buf.getvalue()


@pytest.fixture(scope="session")
def recording_bytes() -> bytes:
    return write_recording(build_events(6000))


@pytest.fixture
def reader(recording_bytes: bytes) -> ChronoReader:
    return ChronoReader.from_bytes(recording_bytes)
