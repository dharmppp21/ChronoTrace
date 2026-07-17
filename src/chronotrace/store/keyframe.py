"""Keyframes: the periodic full-state snapshots that make time travel O(1)-ish.

The video-codec analogy, made real
----------------------------------
A video stores a full frame every so often (a keyframe) and only *differences*
between them, so a player can seek to any moment by jumping to the nearest keyframe
and decoding forward a bounded number of frames -- never from the start. ChronoTrace
does the same with execution: a keyframe is the complete live program state at an
instant, and reaching any `seq` means finding the nearest keyframe at or before it
and replaying **at most one interval** of events. Without keyframes, reaching
`seq` 500,000 replays 500,000 events; with them, it replays at most `interval`.

The exact instant a keyframe represents
---------------------------------------
A keyframe at `seq` S is the live state **after** event S has been applied, never
during it. Events are atomic here -- a CALL has fully pushed its frame, a VAR_WRITE
has fully bound its local -- so a snapshot taken between events is always consistent
and "mid-frame-transition" is not a state that can be captured. Reconstruction to S
therefore uses the keyframe at S directly (replay zero events); to S+k it replays
events S+1..S+k.

Why a keyframe is almost free (the day-14 pool)
-----------------------------------------------
A keyframe stores every live frame's locals as **`ValueRef`s, never values** -- the
content-addressed value pool already holds the values, deduplicated. A local that
has not changed in a million events is one 4-byte ref in every keyframe, not a copy
of the object. So a keyframe is a few structs and a handful of ints per frame; the
expensive part (the values) was paid once, by the pool.

Live frames come from the registry model, not a stack
-----------------------------------------------------
The set of live frames is projected from the event stream exactly as the day-6
`FrameRegistry` projects it: a frame is live from CALL until RETURN/UNWIND, and
YIELD **suspends** it rather than ending it. A suspended generator is still a live
frame with live locals, so it stays in the snapshot -- keyed by `frame_id` in a
*set*, never popped by a stack, which is the whole reason the registry replaced the
stack (get this wrong and reconstruction silently loses a generator's variables).

On-disk layout of a KEYFRAMES block payload (spec §6.6)
------------------------------------------------------
`[u64 seq]` uncompressed (so the reader peeks it without decompressing), then the
compression frame over `[u8 kf_flags][u32 frame_count]` and, per frame,
`[u64 frame_id][u32 code_id][u32 lineno][u8 frame_flags][u32 local_count]` followed
by `local_count x [u32 name_id][u32 value_ref]`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

from chronotrace.recorder.events import Event, EventKind

DEFAULT_KEYFRAME_INTERVAL: Final = 1000
"""Keyframe every N events. The reconstruction-cost contract: reaching any `seq`
replays **at most `interval`** events -- the product's scrubbing-latency guarantee.
Small interval -> bigger file, faster seek; large -> smaller file, slower seek (the
tradeoff curve is in benchmarks/RESULTS.md). 1000 favours seek latency, since that
is what a timeline scrubber feels; day 18 tunes it with the block-size sweep. It is
a first-class knob on the writer, never a constant baked into cadence."""

MAX_FRAMES_PER_KEYFRAME: Final = 4096
"""Trust bound on a keyframe's frame count (and the writer's deep-stack policy cap).
Above CPython's ~1000 recursion limit, so a real program never trips it; a hostile
block claiming more is rejected before its frames are walked, and a genuinely
pathological stack is snapshotted to its innermost `MAX_FRAMES` and flagged."""

MAX_LOCALS_PER_FRAME: Final = 4096
"""Trust bound on one frame's local count. A function with more than this many
locals is pathological; a hostile frame is rejected, a real over-long one truncated
and flagged."""

KF_SEQ = struct.Struct("<Q")
"""The u64 `seq` prefix on a KEYFRAMES payload, uncompressed so the reader can peek
which instant a block snapshots without decompressing it (the seq index)."""
KF_SEQ_SIZE = KF_SEQ.size

_KF_HEADER = struct.Struct("<B I")  # kf_flags, frame_count
_FRAME = struct.Struct("<Q I I B I")  # frame_id, code_id, lineno, frame_flags, local_count
_LOCAL = struct.Struct("<I I")  # name_id, value_ref

_SUSPENDED = 0x01  # frame_flags: the frame is a suspended generator/coroutine
_LOCALS_TRUNCATED = 0x02  # frame_flags: locals were capped at MAX_LOCALS_PER_FRAME
_FRAMES_TRUNCATED = 0x01  # kf_flags: frames were capped at MAX_FRAMES_PER_KEYFRAME


@dataclass(slots=True)
class FrameSnapshot:
    """One live frame at the keyframe instant: where it is, and its locals as refs."""

    frame_id: int
    code_id: int
    lineno: int
    suspended: bool
    local_refs: dict[int, int]  # name_id -> value_ref (into the value pool)
    locals_truncated: bool = False


@dataclass(slots=True)
class Keyframe:
    """Complete live state after event `seq`. What reconstruction starts a replay from."""

    seq: int
    frames: list[FrameSnapshot]
    truncated: bool = False  # frames were dropped (a stack deeper than policy allows)


class LiveState:
    """The running projection of live program state, so the writer can snapshot it.

    Fed every event in `seq` order (`apply`), it maintains the exact set of live
    frames and each frame's current locals -- the day-6 registry model, derived from
    the event stream rather than the runtime, so the store needs nothing from the
    recorder but the event vocabulary. `encode` serialises the current state as a
    keyframe payload. O(1) per event; O(live frames x locals) per snapshot.
    """

    __slots__ = ("_frames",)

    def __init__(self) -> None:
        self._frames: dict[int, FrameSnapshot] = {}

    def apply(self, event: Event) -> None:
        """Fold one event into the live state. Must be called in `seq` order."""
        kind, fid = event.kind, event.frame_id
        if kind == EventKind.CALL:
            self._frames[fid] = FrameSnapshot(fid, event.code_id, event.lineno, False, {})
            return
        if kind in (EventKind.RETURN, EventKind.UNWIND):
            self._frames.pop(fid, None)  # frame is gone; YIELD does NOT reach here
            return
        frame = self._ensure(fid, event)
        if (
            kind == EventKind.VAR_WRITE
            and event.name_id is not None
            and event.value_ref is not None
        ):
            frame.local_refs[event.name_id] = event.value_ref
        elif kind == EventKind.YIELD:
            frame.suspended = True
        elif kind == EventKind.RESUME:
            frame.suspended = False
        if event.lineno:  # kinds without a line carry 0; don't clobber the frame's position
            frame.lineno = event.lineno

    def encode(self) -> bytes:
        """Serialise the current live state as a keyframe payload (spec §6.6)."""
        frames = list(self._frames.values())
        kf_flags = 0
        if len(frames) > MAX_FRAMES_PER_KEYFRAME:
            frames = frames[-MAX_FRAMES_PER_KEYFRAME:]  # keep the innermost; flag the loss
            kf_flags = _FRAMES_TRUNCATED
        out = bytearray(_KF_HEADER.pack(kf_flags, len(frames)))
        for f in frames:
            out += _encode_frame(f)
        return bytes(out)

    def __len__(self) -> int:
        return len(self._frames)

    def _ensure(self, fid: int, event: Event) -> FrameSnapshot:
        """The frame for `fid`, creating it if its CALL was never seen.

        Recording can start mid-execution, so a frame's first event may be a LINE,
        not a CALL. Creating it here means those frames still appear in keyframes.
        """
        frame = self._frames.get(fid)
        if frame is None:
            frame = FrameSnapshot(fid, event.code_id, event.lineno, False, {})
            self._frames[fid] = frame
        return frame


def _encode_frame(f: FrameSnapshot) -> bytes:
    items = list(f.local_refs.items())
    frame_flags = _SUSPENDED if f.suspended else 0
    if len(items) > MAX_LOCALS_PER_FRAME:
        items = items[:MAX_LOCALS_PER_FRAME]
        frame_flags |= _LOCALS_TRUNCATED
    out = bytearray(_FRAME.pack(f.frame_id, f.code_id, f.lineno, frame_flags, len(items)))
    for name_id, value_ref in items:
        out += _LOCAL.pack(name_id, value_ref)
    return bytes(out)


def decode_keyframe(payload: bytes, seq: int) -> Keyframe:
    """Decode a keyframe payload (the bytes after the seq prefix) back to a `Keyframe`.

    **Parses untrusted input.** The frame count and each local count are bounded
    before their regions are read, and every struct read is bounds-checked by
    `struct.error` on a short buffer, so a hostile block cannot over-allocate or
    read out of range.

    Raises:
        ValueError: a frame or local count exceeds its cap.
        struct.error: the payload is too short for a header it declares.
    """
    kf_flags, frame_count = _KF_HEADER.unpack_from(payload, 0)
    if frame_count > MAX_FRAMES_PER_KEYFRAME:
        raise ValueError(f"keyframe claims {frame_count} frames, over the cap")
    pos = _KF_HEADER.size
    frames: list[FrameSnapshot] = []
    for _ in range(frame_count):
        frame, pos = _decode_frame(payload, pos)
        frames.append(frame)
    return Keyframe(seq, frames, bool(kf_flags & _FRAMES_TRUNCATED))


def _decode_frame(payload: bytes, pos: int) -> tuple[FrameSnapshot, int]:
    frame_id, code_id, lineno, frame_flags, local_count = _FRAME.unpack_from(payload, pos)
    if local_count > MAX_LOCALS_PER_FRAME:
        raise ValueError(f"keyframe frame claims {local_count} locals, over the cap")
    pos += _FRAME.size
    if pos + local_count * _LOCAL.size > len(payload):
        raise ValueError("keyframe frame locals overrun the block")
    refs: dict[int, int] = {}
    for _ in range(local_count):
        name_id, value_ref = _LOCAL.unpack_from(payload, pos)
        refs[name_id] = value_ref
        pos += _LOCAL.size
    return (
        FrameSnapshot(
            frame_id,
            code_id,
            lineno,
            bool(frame_flags & _SUSPENDED),
            refs,
            bool(frame_flags & _LOCALS_TRUNCATED),
        ),
        pos,
    )
