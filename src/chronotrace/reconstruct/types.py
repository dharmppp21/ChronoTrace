"""The reconstruct layer's vocabulary: what "program state at an instant" *is*.

`ProgramState` is the DTO the whole product serves. It is deliberately **ids, not
resolved values** (`name_id`/`code_id`/`value_ref`, never a variable name, source line
or Python object): resolution belongs to a higher layer, so `server` can serialise a
state without importing a storage type, and a state stays compact enough to cache and
diff. The instant it describes is the state **after** event `seq` -- identical to a
keyframe (`store.keyframe`), because reconstruction *starts* from one.

Why immutable (frozen), not copy-on-write-by-convention
-------------------------------------------------------
The day-20 reconstructor advances state by pure `apply`/`invert` that return a *new*
state. Freezing the DTO makes that the only option the type allows, which buys two
things the design leans on hard: the day-20 correctness oracle becomes a trivial `==`
(two states are equal iff every field is), and a backward step can *share* the frames it
did not touch instead of deep-copying the whole stack. A frozen state is also safe to
hand across the layer boundary -- `server` cannot mutate a debugger's history by
accident.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from chronotrace.store import Keyframe

NO_FRAME = 0
"""`current_frame_id` when nothing is executing -- before the first frame starts, and
after the last returns. Matches the recorder's `frames.NO_FRAME` sentinel."""


@dataclass(frozen=True, slots=True)
class FrameState:
    """One live frame at an instant: where it is, who called it, and what it holds.

    Attributes:
        frame_id: the recorder's stable per-frame id (never `id(frame)`).
        code_id: interned code object -- resolves to filename/qualname elsewhere.
        lineno: the source line this frame is paused at (executing, or the call/yield it
            is suspended on).
        parent_id: the frame that called this one (the call tree), or None for a root or
            when unknown. A keyframe stores a flat frame *set* with no links, so this is
            filled from CALL events during reconstruction and is authoritative only once
            the day-27 call-tree index exists.
        suspended: a yielded generator/coroutine -- live, with live locals, but not on
            any thread's stack (the day-6 registry model, carried through).
        bindings: `name_id -> value_ref`, the frame's locals as pool references.
    """

    frame_id: int
    code_id: int
    lineno: int
    parent_id: int | None
    suspended: bool
    bindings: Mapping[int, int]


@dataclass(frozen=True, slots=True)
class ExceptionState:
    """An exception in flight at the instant -- raised, not yet unwound past or handled."""

    exc_type_id: int
    raised_at_seq: int
    value_ref: int | None = None


@dataclass(frozen=True, slots=True)
class ProgramState:
    """The complete program state **after** event `seq`. Immutable; see module docstring.

    Attributes:
        seq: the instant. Same semantics as a keyframe: after `seq` executed.
        frames: the live frames, in call order (roots first). A suspended generator is
            present here though it is on no stack.
        current_frame_id: the frame that executed the event at `seq`, or `NO_FRAME`.
        exception: an in-flight exception, or None.
    """

    seq: int
    frames: tuple[FrameState, ...]
    current_frame_id: int
    exception: ExceptionState | None = None

    def frame(self, frame_id: int) -> FrameState | None:
        """The live frame with `frame_id`, or None. O(frames) -- the stack is shallow."""
        return next((f for f in self.frames if f.frame_id == frame_id), None)

    @classmethod
    def from_keyframe(cls, keyframe: Keyframe) -> ProgramState:
        """The reconstruction baseline a keyframe encodes.

        A keyframe knows each live frame's code, line, suspension and bindings, but not
        the call parents, the current-frame pointer, or an in-flight exception -- those
        are overlaid from the events since the keyframe (day 20). So this fills what the
        keyframe knows and leaves `parent_id=None`, `current_frame_id=NO_FRAME`,
        `exception=None` for the overlay to complete. The `seq` carries through unchanged,
        which is what keeps the instant semantics identical to the keyframe's.
        """
        frames = tuple(
            FrameState(f.frame_id, f.code_id, f.lineno, None, f.suspended, dict(f.local_refs))
            for f in keyframe.frames
        )
        return cls(seq=keyframe.seq, frames=frames, current_frame_id=NO_FRAME)

    def as_dict(self) -> dict[str, Any]:
        """The JSON-able shape `server` serves.

        Ids stay ids -- the server resolves them, so no storage type ever crosses the
        wire and this DTO is the whole API contract.
        """
        return {
            "seq": self.seq,
            "current_frame_id": self.current_frame_id,
            "exception": None
            if self.exception is None
            else {
                "exc_type_id": self.exception.exc_type_id,
                "raised_at_seq": self.exception.raised_at_seq,
                "value_ref": self.exception.value_ref,
            },
            "frames": [
                {
                    "frame_id": f.frame_id,
                    "code_id": f.code_id,
                    "lineno": f.lineno,
                    "parent_id": f.parent_id,
                    "suspended": f.suspended,
                    "bindings": dict(f.bindings),
                }
                for f in self.frames
            ],
        }


class Reconstructor(Protocol):
    """Produce the program state at any instant. The one function the product is.

    A `Protocol`, not a base class, for the same reason the day-4 `Sink` is: the day-20
    implementation satisfies it structurally, and a test double is any object with a
    `reconstruct` method -- no inheritance across the layer boundary.
    """

    def reconstruct(self, seq: int) -> ProgramState:
        """The program state after event `seq`.

        Raises:
            IndexError: `seq` is outside `[0, len(recording))` -- including a `seq` in the
                lost tail of a truncated recording, where no state exists.
        """
        ...
