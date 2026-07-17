"""Keyframes: the seq-0 floor, boundary-exact nearest lookup, suspended generators
in the snapshot, hostile-input bounds, and graceful degradation on a torn keyframe."""

from __future__ import annotations

import io
import struct

import pytest

from chronotrace.recorder.capture import CapturedValue, capture
from chronotrace.recorder.events import Event, EventKind
from chronotrace.recorder.values import ValueRef
from chronotrace.store import ChronoReader
from chronotrace.store.keyframe import (
    MAX_FRAMES_PER_KEYFRAME,
    MAX_LOCALS_PER_FRAME,
    LiveState,
    decode_keyframe,
)
from chronotrace.store.writer import ChronoWriter

K = EventKind


def _ev(
    seq: int,
    kind: EventKind,
    fid: int,
    *,
    lineno: int = 0,
    code: int = 1,
    name: int | None = None,
    ref: int | None = None,
) -> Event:
    return Event(
        seq=seq,
        kind=kind,
        timestamp_ns=1000 + seq,
        thread_id=1,
        frame_id=fid,
        code_id=code,
        lineno=lineno,
        name_id=name,
        value_ref=None if ref is None else ValueRef(ref),
    )


def _write(
    events: list[Event], *, interval: int, values: list[CapturedValue] | None = None
) -> bytes:
    buf = io.BytesIO()
    writer = ChronoWriter(buf, keyframe_interval=interval)
    for value in values or []:
        writer.add_value(value)
    for event in events:
        writer.add(event)
    writer.close()
    return buf.getvalue()


def _linear(n: int) -> list[Event]:
    """One frame, n LINE events at seq 0..n-1."""
    return [_ev(s, K.LINE, 1, lineno=1 + s) for s in range(n)]


# ---------------------------------------------------------------------------
# The seq-0 floor and nearest-lookup boundaries
# ---------------------------------------------------------------------------


def test_a_keyframe_at_seq_zero_always_exists() -> None:
    reader = ChronoReader.from_bytes(_write(_linear(50), interval=10))
    assert reader.keyframe_count() == 5  # seq 0, 10, 20, 30, 40
    kf0 = reader.nearest_keyframe_at_or_before(0)
    assert kf0 is not None and kf0.seq == 0


def test_recording_shorter_than_one_interval_has_only_the_seq_zero_keyframe() -> None:
    reader = ChronoReader.from_bytes(_write(_linear(5), interval=1000))
    assert reader.keyframe_count() == 1
    assert reader.nearest_keyframe_at_or_before(4).seq == 0  # type: ignore[union-attr]


def test_no_events_means_no_keyframes() -> None:
    reader = ChronoReader.from_bytes(_write([], interval=10))
    assert reader.keyframe_count() == 0
    assert reader.nearest_keyframe_at_or_before(0) is None


def test_nearest_keyframe_is_correct_at_every_boundary() -> None:
    reader = ChronoReader.from_bytes(_write(_linear(35), interval=10))  # kfs at 0,10,20,30
    cases = {
        0: 0,
        5: 0,
        9: 0,  # before the second keyframe
        10: 10,
        11: 10,
        19: 10,  # exactly on, and after
        30: 30,
        34: 30,  # the last keyframe, and past every event
        100: 30,  # well beyond the recording
    }
    for target, expected in cases.items():
        kf = reader.nearest_keyframe_at_or_before(target)
        assert kf is not None and kf.seq == expected, f"nearest<={target} should be {expected}"


def test_nearest_before_the_first_keyframe_is_none() -> None:
    reader = ChronoReader.from_bytes(_write(_linear(5), interval=10))
    assert reader.nearest_keyframe_at_or_before(-1) is None  # nothing at or before -1


# ---------------------------------------------------------------------------
# Snapshot content: live frames, accumulated locals, suspended generators
# ---------------------------------------------------------------------------


def test_locals_accumulate_and_a_returned_frame_is_gone() -> None:
    events = [
        _ev(0, K.CALL, 1, lineno=1),
        _ev(1, K.VAR_WRITE, 1, lineno=2, name=10, ref=100),
        _ev(2, K.CALL, 2, lineno=5, code=2),
        _ev(3, K.VAR_WRITE, 1, lineno=3, name=11, ref=101),
        _ev(4, K.RETURN, 2, lineno=6),  # frame 2 returns -- gone from the next keyframe
    ]
    reader = ChronoReader.from_bytes(_write(events, interval=1))  # a keyframe at every seq
    kf = reader.nearest_keyframe_at_or_before(4)
    assert kf is not None
    frames = {f.frame_id: f for f in kf.frames}
    assert set(frames) == {1}  # frame 2 returned; only frame 1 is live
    assert frames[1].local_refs == {10: 100, 11: 101}  # both writes accumulated
    assert frames[1].lineno == 3


def test_a_suspended_generator_frame_stays_in_the_snapshot_with_its_locals() -> None:
    """The correctness case: YIELD suspends a frame, it does NOT end it. Its locals are
    still live state and must be in the keyframe -- get this wrong and reconstruction
    silently loses a generator's variables."""
    events = [
        _ev(0, K.CALL, 1, lineno=1),
        _ev(1, K.CALL, 2, lineno=10, code=2),  # generator starts
        _ev(2, K.VAR_WRITE, 2, lineno=11, name=5, ref=42),  # x = <ref 42>
        _ev(3, K.YIELD, 2, lineno=11),  # suspends -- still alive
        _ev(4, K.LINE, 1, lineno=2),  # back in the caller
    ]
    reader = ChronoReader.from_bytes(_write(events, interval=1))
    kf = reader.nearest_keyframe_at_or_before(4)
    assert kf is not None
    frames = {f.frame_id: f for f in kf.frames}
    assert set(frames) == {1, 2}, "the suspended generator frame must still be live"
    assert frames[2].suspended is True
    assert frames[2].local_refs == {5: 42}, "the generator's local survived the yield"


def test_a_resumed_generator_is_no_longer_suspended() -> None:
    events = [
        _ev(0, K.CALL, 2, lineno=10, code=2),
        _ev(1, K.YIELD, 2, lineno=10),
        _ev(2, K.RESUME, 2, lineno=10),
    ]
    reader = ChronoReader.from_bytes(_write(events, interval=1))
    kf = reader.nearest_keyframe_at_or_before(2)
    assert kf is not None and kf.frames[0].suspended is False


def test_keyframe_refs_resolve_through_the_value_pool() -> None:
    """A keyframe stores ValueRefs; the day-14 pool holds the values. Both must line up."""
    val = capture({"balance": 250})
    buf = io.BytesIO()
    writer = ChronoWriter(buf, keyframe_interval=1)
    ref = writer.add_value(val)
    writer.add(_ev(0, K.CALL, 1, lineno=1))
    writer.add(_ev(1, K.VAR_WRITE, 1, lineno=2, name=3, ref=ref))
    writer.close()

    reader = ChronoReader.from_bytes(buf.getvalue())
    kf = reader.nearest_keyframe_at_or_before(1)
    assert kf is not None
    stored_ref = kf.frames[0].local_refs[3]
    assert reader.value(stored_ref) == val  # the ref resolves to the captured value


# ---------------------------------------------------------------------------
# LiveState unit round-trip, and the deep-stack policy bound
# ---------------------------------------------------------------------------


def test_livestate_encode_decode_round_trips() -> None:
    live = LiveState()
    for e in [
        _ev(0, K.CALL, 1, lineno=1),
        _ev(1, K.VAR_WRITE, 1, lineno=2, name=7, ref=70),
        _ev(2, K.CALL, 9, lineno=5, code=3),
        _ev(3, K.YIELD, 9, lineno=6),
    ]:
        live.apply(e)
    kf = decode_keyframe(live.encode(), seq=3)
    frames = {f.frame_id: f for f in kf.frames}
    assert frames[1].local_refs == {7: 70}
    assert frames[9].suspended is True
    assert kf.truncated is False


def test_a_stack_deeper_than_policy_is_truncated_and_flagged() -> None:
    live = LiveState()
    for fid in range(MAX_FRAMES_PER_KEYFRAME + 50):
        live.apply(_ev(fid, K.CALL, fid + 1, lineno=1))  # frame_id 0 is NO_FRAME; start at 1
    kf = decode_keyframe(live.encode(), seq=0)
    assert kf.truncated is True
    assert len(kf.frames) == MAX_FRAMES_PER_KEYFRAME  # bounded, innermost kept


# ---------------------------------------------------------------------------
# decode_keyframe parses untrusted input
# ---------------------------------------------------------------------------


def test_decode_rejects_a_huge_frame_count() -> None:
    forged = struct.pack("<B I", 0, MAX_FRAMES_PER_KEYFRAME + 1)
    with pytest.raises(ValueError, match="over the cap"):
        decode_keyframe(forged, seq=0)


def test_decode_rejects_a_frame_with_too_many_locals() -> None:
    # one frame declaring local_count over the cap
    forged = struct.pack("<B I", 0, 1) + struct.pack(
        "<Q I I B I", 1, 1, 1, 0, MAX_LOCALS_PER_FRAME + 1
    )
    with pytest.raises(ValueError, match="over the cap"):
        decode_keyframe(forged, seq=0)


def test_decode_rejects_locals_that_overrun_the_block() -> None:
    # one frame claiming 100 locals but carrying no local bytes
    forged = struct.pack("<B I", 0, 1) + struct.pack("<Q I I B I", 1, 1, 1, 0, 100)
    with pytest.raises(ValueError, match="overrun"):
        decode_keyframe(forged, seq=0)


# ---------------------------------------------------------------------------
# Graceful degradation: a torn keyframe falls back to the previous one
# ---------------------------------------------------------------------------


def test_a_corrupt_keyframe_degrades_to_the_previous_one() -> None:
    data = bytearray(_write(_linear(35), interval=10))  # keyframes at 0,10,20,30
    probe = ChronoReader.from_bytes(bytes(data))
    assert probe.keyframe_count() == 4
    # Corrupt the block of the keyframe at seq 30 (the last one): flip a byte in its
    # compressed body, past the frame header (12) and the u64 seq prefix (8).
    _seq, offset = probe._keyframes[-1]  # test reaches in for the block offset
    data[offset + 12 + 8 + 2] ^= 0xFF

    reader = ChronoReader.from_bytes(bytes(data))
    kf = reader.nearest_keyframe_at_or_before(34)  # would pick seq 30, which is now torn
    assert kf is not None and kf.seq == 20, "fell back to the previous intact keyframe"
