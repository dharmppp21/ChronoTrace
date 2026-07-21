"""The delta's referee is one property: `invert(apply(s, d)) == s`. If it holds for
random states and valid deltas, the delta is lossless and reversible -- which is what
lets reconstruction step backward. The rest pins the explicit cases and the bounds."""

from __future__ import annotations

import copy
import io
import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from chronotrace.recorder.events import Event, EventKind
from chronotrace.recorder.values import ValueRef
from chronotrace.store import ChronoReader
from chronotrace.store.delta import (
    MAX_DELTAS_PER_BLOCK,
    NO_REF,
    Delta,
    DeltaError,
    DeltaKind,
    State,
    apply,
    decode_deltas,
    derive,
    encode_deltas,
    inverse,
    invert,
    state_from_keyframe,
)
from chronotrace.store.keyframe import FrameSnapshot, LiveState
from chronotrace.store.writer import ChronoWriter

K = EventKind

_refs = st.integers(min_value=0, max_value=40)
_names = st.integers(min_value=0, max_value=15)
_fids = st.integers(min_value=1, max_value=8)


@st.composite
def _states(draw: st.DrawFn) -> State:
    fids = draw(st.lists(_fids, unique=True, max_size=4))
    return {fid: draw(st.dictionaries(_names, _refs, max_size=5)) for fid in fids}


@st.composite
def _state_and_valid_delta(draw: st.DrawFn) -> tuple[State, Delta]:
    """A random state and a delta that is *legal* for it -- the domain over which
    invertibility must hold (a delta whose `old` disagrees with the state is not a
    transition from that state and is not required to round-trip)."""
    state = draw(_states())
    live = list(state)
    seq = draw(st.integers(0, 1000))
    kinds = ["enter"]
    if live:
        kinds += ["exit", "bind_new"]
    if any(state[f] for f in live):
        kinds += ["rebind", "delete"]
    kind = draw(st.sampled_from(kinds))

    if kind == "enter":
        new_fid = draw(_fids.filter(lambda f: f not in state))
        locs = tuple(draw(st.dictionaries(_names, _refs, max_size=3)).items())
        return state, Delta(DeltaKind.FRAME_ENTER, seq, new_fid, frame_locals=locs)
    if kind == "exit":
        fid = draw(st.sampled_from(live))
        return state, Delta(DeltaKind.FRAME_EXIT, seq, fid, frame_locals=tuple(state[fid].items()))
    bound = [f for f in live if state[f]]
    fid = draw(st.sampled_from(bound if kind in ("rebind", "delete") else live))
    if kind == "bind_new":
        name = draw(_names.filter(lambda n: n not in state[fid]))
        return state, Delta(DeltaKind.BIND, seq, fid, name, NO_REF, draw(_refs))
    name = draw(st.sampled_from(list(state[fid])))
    old = state[fid][name]
    new = NO_REF if kind == "delete" else draw(_refs)
    return state, Delta(DeltaKind.BIND, seq, fid, name, old, new)


# ---------------------------------------------------------------------------
# The referee
# ---------------------------------------------------------------------------


@given(_state_and_valid_delta())
def test_invert_undoes_apply_and_apply_is_pure(sd: tuple[State, Delta]) -> None:
    state, delta = sd
    original = copy.deepcopy(state)
    forward = apply(state, delta)
    assert invert(forward, delta) == original, "invert(apply(s, d)) must equal s"
    assert state == original, "apply must not mutate its input (it is pure)"


# ---------------------------------------------------------------------------
# Explicit cases: created / deleted / rebound / enter / exit
# ---------------------------------------------------------------------------


def test_a_created_binding_inverts_to_a_deletion() -> None:
    s: State = {1: {}}
    create = Delta(DeltaKind.BIND, 0, 1, 5, NO_REF, 99)
    assert apply(s, create) == {1: {5: 99}}
    assert inverse(create) == Delta(DeltaKind.BIND, 0, 1, 5, 99, NO_REF)  # a delete
    assert invert(apply(s, create), create) == s


def test_a_deleted_binding_inverts_to_a_creation() -> None:
    s = {1: {5: 99}}
    delete = Delta(DeltaKind.BIND, 0, 1, 5, 99, NO_REF)
    assert apply(s, delete) == {1: {}}  # del removes the name
    assert invert(apply(s, delete), delete) == s


def test_a_rebind_round_trips() -> None:
    s = {1: {5: 99}}
    rebind = Delta(DeltaKind.BIND, 0, 1, 5, 99, 100)
    assert apply(s, rebind) == {1: {5: 100}}
    assert invert(apply(s, rebind), rebind) == s


def test_frame_enter_then_exit_restores_the_frame_with_its_locals() -> None:
    s: State = {}
    enter = Delta(DeltaKind.FRAME_ENTER, 0, 7)
    s2 = apply(s, enter)
    assert s2 == {7: {}}
    exit_ = Delta(DeltaKind.FRAME_EXIT, 5, 7, frame_locals=((5, 50), (6, 60)))
    s3 = apply({7: {5: 50, 6: 60}}, exit_)
    assert s3 == {}
    assert invert(s3, exit_) == {7: {5: 50, 6: 60}}  # exit carries locals, so it restores them


# ---------------------------------------------------------------------------
# Out-of-order application must raise, never invent state
# ---------------------------------------------------------------------------


def test_apply_to_a_missing_or_duplicate_frame_raises() -> None:
    with pytest.raises(DeltaError):
        apply({}, Delta(DeltaKind.BIND, 0, 9, 1, NO_REF, 2))  # bind to a dead frame
    with pytest.raises(DeltaError):
        apply({1: {}}, Delta(DeltaKind.FRAME_ENTER, 0, 1))  # enter an already-live frame
    with pytest.raises(DeltaError):
        apply({}, Delta(DeltaKind.FRAME_EXIT, 0, 9))  # exit a frame that is not live


# ---------------------------------------------------------------------------
# Closures: the delta follows the event's frame_id, faithfully
# ---------------------------------------------------------------------------


def test_a_write_is_attributed_to_the_events_frame_including_an_enclosing_one() -> None:
    """A nonlocal write mutates an *enclosing* frame's binding. The recorder decides
    which frame_id the write belongs to (it scans that frame's f_locals); the delta is
    faithful to that frame_id and never re-homes the write. Here the event names the
    enclosing frame (2), so the bind lands on frame 2, not the inner frame (5)."""
    frames = {2: FrameSnapshot(2, 1, 1, False, {10: 100}), 5: FrameSnapshot(5, 2, 1, False, {})}
    write_to_enclosing = Event(
        seq=9,
        kind=K.VAR_WRITE,
        timestamp_ns=1,
        thread_id=1,
        frame_id=2,
        code_id=1,
        lineno=3,
        name_id=10,
        value_ref=ValueRef(200),
    )
    (delta,) = derive(write_to_enclosing, frames)
    assert delta.frame_id == 2 and delta.old_ref == 100 and delta.new_ref == 200


def test_a_var_write_with_no_value_is_a_deletion() -> None:
    """`del x` on the wire: a VAR_WRITE carrying no `value_ref` (issue #7).

    It needs no new delta kind and no layout change, because `NO_REF` is already how a
    BIND spells a deletion and the column already held `-1` for events with no value.
    `old_ref` still carries the value being removed, so the delta stays invertible --
    stepping backward over a `del` has to put the binding back.
    """
    frames = {4: FrameSnapshot(4, 1, 1, False, {7: 99})}
    deleted = _ev(5, K.VAR_WRITE, 4, name=7, ref=None)
    (delta,) = derive(deleted, frames)
    assert (delta.kind, delta.old_ref, delta.new_ref) == (DeltaKind.BIND, 99, NO_REF)
    assert apply({4: {7: 99}}, delta) == {4: {}}
    assert invert(apply({4: {7: 99}}, delta), delta) == {4: {7: 99}}


def test_the_writer_and_the_delta_stream_agree_about_a_deletion() -> None:
    """`LiveState` (which builds keyframes) and `derive` (which builds deltas) are the two
    halves of the codec, and both read the *same* rule from `binding_change`.

    If they ever disagreed about whether an event binds, a keyframe would claim one state
    and replaying the deltas would produce another. That divergence is why the rule lives
    in one function rather than being written out twice.
    """
    live = LiveState()
    state: State = {}
    for event in (
        _ev(0, K.CALL, 4),
        _ev(1, K.VAR_WRITE, 4, name=7, ref=99),
        _ev(2, K.VAR_WRITE, 4, name=7, ref=None),
    ):
        for delta in derive(event, live.frames):
            state = apply(state, delta)
        live.apply(event)
    assert state == {4: {}}
    assert live.frames[4].local_refs == {}


def test_a_write_to_a_frame_seen_first_mid_recording_gets_an_implicit_enter() -> None:
    """Recording can begin mid-execution, so a frame's first event may be a bind, not a
    call. derive must precede that bind with a frame-enter, or applying the delta stream
    to a fresh keyframe state would raise on a frame that was never entered."""
    write = _ev(3, K.VAR_WRITE, 42, name=1, ref=7)
    deltas = derive(write, {})  # frame 42 is unknown -- no prior CALL seen
    assert [d.kind for d in deltas] == [DeltaKind.FRAME_ENTER, DeltaKind.BIND]
    assert deltas[1].old_ref == NO_REF  # a brand-new binding
    state: State = {}
    for d in deltas:
        state = apply(state, d)
    assert state == {42: {1: 7}}  # the stream applies cleanly from empty


# ---------------------------------------------------------------------------
# derive from the event stream, and forward reconstruction from a keyframe
# ---------------------------------------------------------------------------


def _ev(
    seq: int,
    kind: EventKind,
    fid: int,
    *,
    name: int | None = None,
    ref: int | None = None,
    lineno: int = 1,
    code: int = 1,
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


_STREAM = [
    _ev(0, K.CALL, 1),
    _ev(1, K.VAR_WRITE, 1, name=10, ref=100),
    _ev(2, K.CALL, 2, code=2),
    _ev(3, K.VAR_WRITE, 2, name=20, ref=200),
    _ev(4, K.VAR_WRITE, 1, name=10, ref=101),  # rebind
    _ev(5, K.RETURN, 2),  # frame 2 gone
    _ev(6, K.VAR_WRITE, 1, name=11, ref=102),
]


def _truth_at(target: int) -> State:
    live = LiveState()
    for event in _STREAM[: target + 1]:
        live.apply(event)
    return {fid: dict(f.local_refs) for fid, f in live.frames.items()}


def _write(stream: list[Event], *, interval: int) -> bytes:
    buf = io.BytesIO()
    writer = ChronoWriter(buf, keyframe_interval=interval)
    for event in stream:
        writer.add(event)
    writer.close()
    return buf.getvalue()


def test_keyframe_plus_deltas_reconstructs_every_instant() -> None:
    reader = ChronoReader.from_bytes(_write(_STREAM, interval=2))  # keyframes at 0,2,4,6
    for target in range(len(_STREAM)):
        kf = reader.nearest_keyframe_at_or_before(target)
        assert kf is not None
        state = state_from_keyframe(kf)
        for delta in reader.deltas_between(kf.seq + 1, target):
            state = apply(state, delta)
        assert state == _truth_at(target), f"reconstruction mismatch at seq {target}"


def test_a_single_backward_step_uses_invert_not_a_keyframe_rewind() -> None:
    reader = ChronoReader.from_bytes(_write(_STREAM, interval=100))  # only the seq-0 keyframe
    kf = reader.nearest_keyframe_at_or_before(6)
    assert kf is not None
    state = state_from_keyframe(kf)
    for delta in reader.deltas_between(kf.seq + 1, 6):
        state = apply(state, delta)
    last = reader.deltas_between(6, 6)[-1]
    assert invert(state, last) == _truth_at(5)  # stepped 6 -> 5 by inverting one delta


# ---------------------------------------------------------------------------
# The reconstruction-cost bound, asserted rather than documented
# ---------------------------------------------------------------------------


def test_reconstruction_applies_at_most_interval_deltas() -> None:
    interval = 8
    stream = [_ev(0, K.CALL, 1)] + [
        _ev(s, K.VAR_WRITE, 1, name=s % 4, ref=s) for s in range(1, 200)
    ]
    reader = ChronoReader.from_bytes(_write(stream, interval=interval))
    for target in range(len(stream)):
        kf = reader.nearest_keyframe_at_or_before(target)
        assert kf is not None
        n_deltas = len(reader.deltas_between(kf.seq + 1, target))
        assert n_deltas <= interval, f"seq {target}: {n_deltas} deltas exceeds the interval bound"


# ---------------------------------------------------------------------------
# Serialisation round-trip and hostile-input bounds
# ---------------------------------------------------------------------------


def test_encode_decode_round_trips_all_kinds() -> None:
    deltas = [
        Delta(DeltaKind.FRAME_ENTER, 0, 1),
        Delta(DeltaKind.BIND, 1, 1, 10, NO_REF, 100),
        Delta(DeltaKind.BIND, 2, 1, 10, 100, 101),
        Delta(DeltaKind.BIND, 3, 1, 10, 101, NO_REF),
        Delta(DeltaKind.FRAME_EXIT, 4, 1, frame_locals=((5, 50), (6, 60))),
    ]
    assert decode_deltas(encode_deltas(deltas)) == deltas


def test_decode_rejects_a_delta_count_over_the_cap() -> None:
    forged = struct.pack("<I", MAX_DELTAS_PER_BLOCK + 1)
    with pytest.raises(ValueError, match="over the"):
        decode_deltas(forged)


def test_decode_rejects_inconsistent_locals_region() -> None:
    from chronotrace.store.columnar import pack_columns

    # one FRAME_EXIT claiming 3 local pairs, but the pairs region declares 0
    header = pack_columns([[int(DeltaKind.FRAME_EXIT)], [0], [1], [-1], [-1], [-1], [3]])
    forged = struct.pack("<I", 1) + header + struct.pack("<I", 0) + pack_columns([[], []])
    with pytest.raises(ValueError, match="do not match"):
        decode_deltas(forged)


def test_a_frame_first_seen_without_a_call_is_entered_in_the_delta_stream() -> None:
    """Recording can begin mid-execution, so a frame's first event may be a LINE, not a
    CALL. `LiveState` creates such a frame on sight, so `derive` must ENTER it in the
    delta stream too -- otherwise a later BIND has no frame and a from-zero replay raises
    where a from-keyframe replay (which inherits the frame from the snapshot) succeeds.
    Found on day 20 by the reconstruction oracle, on a real recording."""
    frames: dict[int, FrameSnapshot] = {}
    line = _ev(0, K.LINE, 7, lineno=3)
    deltas = derive(line, frames)
    assert [d.kind for d in deltas] == [DeltaKind.FRAME_ENTER]

    # Replaying only the deltas must now leave frame 7 live, so a later bind applies.
    state: dict[int, dict[int, int]] = {}
    for d in deltas:
        state = apply(state, d)
    assert 7 in state
    state = apply(state, Delta(DeltaKind.BIND, 1, 7, 5, NO_REF, 42))
    assert state[7] == {5: 42}
