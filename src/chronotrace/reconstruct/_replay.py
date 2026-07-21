"""Shared replay machinery: fold deltas and events into a working state, then freeze it.

Both reconstruction paths use exactly this code to *apply* changes -- the fast
keyframe+window path (`reconstructor.py`) and the slow from-zero oracle (`oracle.py`).
They differ only in the **range** they replay, and that is deliberate: the differential
test then isolates the thing that can actually be wrong (which keyframe, which delta
range, an off-by-one at a boundary) rather than re-testing the update rules twice. The
update rules themselves are proven against *reality* by day 22's replay-equivalence
harness, which is the other half of the correctness story.

Division of labour, per ADR-0006
--------------------------------
**Deltas carry data-flow** -- the bindings, and which frames are live -- and are applied
with the store's own `apply`, so there is exactly one implementation of what a delta
means and the forward path cannot drift from the inverse day 21 will use.

**Events carry control-flow** -- each frame's current line, suspension, and the in-flight
exception. These are what the delta stream deliberately omits (a per-line delta would
explode the stream), so they are overlaid from the same bounded window of events. Call
*parents* are deliberately absent: see `overlay_events`.

A working state is mutable and internal; `freeze` turns it into the immutable
`ProgramState` that crosses the layer boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from chronotrace.reconstruct.types import ExceptionState, FrameState, ProgramState
from chronotrace.recorder.events import Event, EventKind
from chronotrace.store import Delta, Keyframe, apply, state_from_keyframe
from chronotrace.store.delta import State


@dataclass(slots=True)
class _Meta:
    """A frame's control-flow metadata -- everything about a frame that is not a binding."""

    code_id: int
    lineno: int
    suspended: bool


@dataclass(slots=True)
class Work:
    """A reconstruction in progress.

    `bindings` is the store's own delta state, so it is both the data *and* the
    authoritative live-frame set (a frame is live iff it has an entry). `meta` is a
    lookup for control-flow; an entry may be missing (a frame from a keyframe that no
    event has touched) or stale (a frame that has since exited), so `freeze` reads it
    only for frames `bindings` says are live.
    """

    bindings: State = field(default_factory=dict)
    meta: dict[int, _Meta] = field(default_factory=dict)
    exception: ExceptionState | None = None


def empty_work() -> Work:
    """A working state before any event -- where the oracle starts."""
    return Work()


def work_from_keyframe(keyframe: Keyframe) -> Work:
    """The baseline a keyframe encodes: bindings, and each frame's code/line/suspension.

    The in-flight exception *is* in the keyframe (it must be, or reconstruction would be
    path-dependent), so it carries through here.
    """
    exception = (
        ExceptionState(keyframe.exception[0], keyframe.exception[1])
        if keyframe.exception is not None
        else None
    )
    return Work(
        bindings=state_from_keyframe(keyframe),
        meta={f.frame_id: _Meta(f.code_id, f.lineno, f.suspended) for f in keyframe.frames},
        exception=exception,
    )


def work_from_state(state: ProgramState) -> Work:
    """Reopen a frozen state so it can be advanced -- the locality cache's resume path."""
    return Work(
        bindings={f.frame_id: dict(f.bindings) for f in state.frames},
        meta={f.frame_id: _Meta(f.code_id, f.lineno, f.suspended) for f in state.frames},
        exception=state.exception,
    )


def apply_deltas(work: Work, deltas: Iterable[Delta]) -> None:
    """Fold the data-flow changes in, using the store's own `apply`.

    Never a second copy of delta semantics. A delta naming a frame that is not live raises
    `DeltaError`, which is correct: an inconsistent stream must surface, never invent a
    frame.
    """
    for delta in deltas:
        work.bindings = apply(work.bindings, delta)


def overlay_events(work: Work, events: Iterable[Event]) -> None:
    """Fold the control-flow the deltas omit: lines, suspension, exceptions.

    Call parents are deliberately NOT tracked here: they would be known only for frames
    that entered inside the window, making the value depend on where reconstruction
    started -- and a state that differs by *how you got there* is not state. The call
    tree comes from the day-27 index instead (see ADR-0006's amendment).
    """
    for event in events:
        fid, kind = event.frame_id, event.kind
        if kind == EventKind.CALL:
            work.meta[fid] = _Meta(event.code_id, event.lineno, False)
        else:
            meta = work.meta.get(fid)
            if meta is None:
                meta = work.meta[fid] = _Meta(event.code_id, event.lineno, False)
            if event.lineno:  # kinds without a line carry 0; don't clobber the position
                meta.lineno = event.lineno
            if kind == EventKind.YIELD:
                meta.suspended = True
            elif kind == EventKind.RESUME:
                meta.suspended = False
        if kind == EventKind.RAISE and event.exc_type_id is not None:
            work.exception = ExceptionState(event.exc_type_id, event.seq)
        elif kind == EventKind.EXCEPTION_HANDLED:
            work.exception = None


def freeze(work: Work, seq: int, current_frame_id: int) -> ProgramState:
    """Turn the working state into the immutable `ProgramState` for instant `seq`.

    `bindings` decides who is live and in what order (insertion order is call order);
    `meta` only decorates. A live frame with no metadata yet is impossible in a
    well-formed recording, but is rendered with zeros rather than dropped -- losing a
    frame would understate the stack, which is worse than an unknown line.
    """
    frames = []
    for fid, bindings in work.bindings.items():
        meta = work.meta.get(fid)
        frames.append(
            FrameState(
                frame_id=fid,
                code_id=meta.code_id if meta else 0,
                lineno=meta.lineno if meta else 0,
                suspended=meta.suspended if meta else False,
                bindings=bindings,
            )
        )
    return ProgramState(
        seq=seq,
        frames=tuple(frames),
        current_frame_id=current_frame_id,
        exception=work.exception,
    )
