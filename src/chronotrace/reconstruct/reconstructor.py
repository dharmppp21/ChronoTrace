"""The product, as a function: any `seq` to the program state at that instant.

The algorithm ([ADR-0006](../../../docs/adr/0006-reconstruction.md)), in four lines:

    kf    = nearest_keyframe_at_or_before(seq)   # O(log K)  binary search
    work  = decode(kf)                           # O(F + B)  live frames and bindings
    apply the deltas in (kf.seq, seq]            # <= I      bounded by the cadence
    overlay the events in (kf.seq, seq]          # <= I      control flow

Total **O(log K + I + F + B)** -- a binary search plus a bounded loop, sub-linear in
recording length. The `<= I` bound is proven in ADR-0006 from the day-15 cadence
invariant: keyframes sit at every multiple of `I`, so `seq - kf.seq <= I - 1`.

*To reach `seq` 500,123 we binary-search to keyframe 500,000 and apply the <= 123 deltas
since -- at most 1,000 by the cadence, never 500,123.*

The locality cache
------------------
Scrubbing is local: the user drags one event at a time. So the last state is kept and
reused whenever it is a **cheaper-or-equal starting point than the keyframe** -- that is,
whenever it sits in `[kf.seq, seq]`. Replaying from it is then never more work than
replaying from the keyframe, so this is ADR-0006's window rule stated exactly rather than
approximated by a constant. Anything else (a backward jump, a jump past a keyframe)
restarts from the keyframe, which costs `<= I` by the bound above.

The recording is append-only and immutable, so a cached state never goes *stale*; the
risk is **drift** -- an incrementally advanced state that is subtly wrong is still a
plausible state, which is the one failure a debugger cannot survive. That is why
`tests/reconstruct/test_differential.py` asserts the cached path equals the uncached path
and both equal the oracle.
"""

from __future__ import annotations

from chronotrace.reconstruct._replay import (
    Work,
    apply_deltas,
    empty_work,
    freeze,
    overlay_events,
    work_from_keyframe,
    work_from_state,
)
from chronotrace.reconstruct.types import ProgramState
from chronotrace.store import ChronoReader


class KeyframeReconstructor:
    """Reconstructs program state from keyframes plus a bounded delta replay.

    Satisfies the `Reconstructor` protocol. Holds a `ChronoReader` (the store's typed
    surface -- never a raw block) and one cached `ProgramState`, so its memory is
    `O(live frames + bindings)`, bounded by the traced program's shape rather than by the
    recording's length.
    """

    __slots__ = ("_cache", "_reader", "_use_cache")

    def __init__(self, reader: ChronoReader, *, use_cache: bool = True) -> None:
        """Build a reconstructor over an open recording.

        Args:
            reader: the open recording.
            use_cache: keep the locality cache. Off is the uncached reference path the
                tests compare against -- a drifting cache must be detectable.
        """
        self._reader = reader
        self._use_cache = use_cache
        self._cache: ProgramState | None = None

    def reconstruct(self, seq: int) -> ProgramState:
        """The program state after event `seq`.

        Raises:
            IndexError: `seq` is outside `[0, len(recording))` -- including a `seq` in the
                lost tail of a truncated recording. Never clamped: clamping would invent
                a state the program was never in.
        """
        if not 0 <= seq < len(self._reader):
            raise IndexError(f"seq {seq} out of range [0, {len(self._reader)})")
        keyframe = self._reader.nearest_keyframe_at_or_before(seq)
        floor = keyframe.seq if keyframe is not None else 0

        cached = self._cache if self._use_cache else None
        if cached is not None and floor <= cached.seq <= seq:
            work, start = work_from_state(cached), cached.seq
        elif keyframe is not None:
            work, start = work_from_keyframe(keyframe), keyframe.seq
        else:
            work, start = empty_work(), -1  # no keyframe survived: replay from the start

        state = self._replay(work, start, seq)
        if self._use_cache:
            self._cache = state
        return state

    def _replay(self, work: Work, start: int, seq: int) -> ProgramState:
        """Advance `work` from the instant `start` to `seq` and freeze it.

        The window is `(start, seq]` -- exclusive of `start`, because a keyframe (and a
        cached state) is the state *after* its own event, so re-applying that event would
        double-count it. This is the off-by-one the differential test exists to catch.
        """
        if seq > start:
            apply_deltas(work, self._reader.deltas_between(start + 1, seq))
            overlay_events(work, self._reader[start + 1 : seq + 1])  # type: ignore[arg-type]
        current = self._reader[seq].frame_id  # type: ignore[union-attr]
        return freeze(work, seq, current)
