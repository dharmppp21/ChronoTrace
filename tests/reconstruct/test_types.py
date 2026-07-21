"""The reconstruct DTOs: immutable, instant-identical to keyframes, and a stable wire
shape. The reconstruction *algorithm* is ADR-0006 and lands day 20; these are its types."""

from __future__ import annotations

import dataclasses

import pytest

from chronotrace.reconstruct.types import (
    NO_FRAME,
    ExceptionState,
    FrameState,
    ProgramState,
    Reconstructor,
)
from chronotrace.store.keyframe import FrameSnapshot, Keyframe


def _state() -> ProgramState:
    return ProgramState(
        seq=42,
        frames=(
            FrameState(1, 10, 5, False, {100: 200}),
            FrameState(2, 11, 9, True, {101: 201, 102: 202}),
        ),
        current_frame_id=2,
        exception=ExceptionState(exc_type_id=7, raised_at_seq=40, value_ref=99),
    )


def test_program_state_is_immutable() -> None:
    state = _state()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.seq = 43  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.frames[0].lineno = 6  # type: ignore[misc]


def test_frame_lookup() -> None:
    state = _state()
    assert state.frame(2).frame_id == 2  # type: ignore[union-attr]
    assert state.frame(999) is None


def test_from_keyframe_keeps_the_keyframe_instant_and_bindings() -> None:
    """The one word that must not drift: `seq` -- the state *after* `seq`, same as the
    keyframe. And the keyframe's flat frame set has no parents/current/exception, so the
    baseline leaves those for the day-20 overlay to fill."""
    kf = Keyframe(
        seq=1000,
        frames=[
            FrameSnapshot(1, 10, 5, False, {100: 200}),
            FrameSnapshot(2, 11, 9, True, {101: 201}),
        ],
    )
    state = ProgramState.from_keyframe(kf)
    assert state.seq == kf.seq  # identical instant semantics
    assert state.current_frame_id == NO_FRAME
    assert state.exception is None
    assert [f.frame_id for f in state.frames] == [1, 2]
    assert state.frames[1].suspended is True  # a suspended generator stays live
    assert state.frames[0].bindings == {100: 200}


def test_a_trivial_fake_satisfies_the_reconstructor_protocol() -> None:
    class FakeReconstructor:
        def reconstruct(self, seq: int) -> ProgramState:
            return ProgramState(seq=seq, frames=(), current_frame_id=NO_FRAME)

    def use(r: Reconstructor, seq: int) -> ProgramState:  # mypy checks structural fit
        return r.reconstruct(seq)

    assert use(FakeReconstructor(), 7).seq == 7


def test_as_dict_is_a_stable_id_based_wire_shape() -> None:
    """Golden DTO: ids stay ids (server resolves them), so no storage type crosses the
    wire. A change here is a change to the API contract and must be deliberate."""
    assert _state().as_dict() == {
        "seq": 42,
        "current_frame_id": 2,
        "exception": {"exc_type_id": 7, "raised_at_seq": 40, "value_ref": 99},
        "frames": [
            {
                "frame_id": 1,
                "code_id": 10,
                "lineno": 5,
                "suspended": False,
                "bindings": {100: 200},
            },
            {
                "frame_id": 2,
                "code_id": 11,
                "lineno": 9,
                "suspended": True,
                "bindings": {101: 201, 102: 202},
            },
        ],
    }
