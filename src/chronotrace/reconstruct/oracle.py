"""The slow, obviously-correct reconstruction -- intentionally slow.

> It is the truth the fast path is tested against. **Do not optimise it.** An
> unexplained slow function gets "helpfully" made fast by a future contributor, and the
> moment it shares the fast path's cleverness it stops being able to catch the fast
> path's bugs.

Why the slow one is written *first*
-----------------------------------
`reconstruct_slow` replays every delta and event from `seq` 0. It is O(seq) and cannot
plausibly be wrong: there is no keyframe to decode, no window to get off by one, no cache
to drift. Writing it first buys a *truth to test against before writing the version that
can be subtly wrong* -- and it costs an hour. The fast path is then not "hopefully
right", it is `== ` an implementation whose correctness is obvious by inspection.

This is differential testing, and the oracle **ships**: it lives in the package rather
than in the test tree because the test suite needs it forever (day 21's backward stepping
and day 22's harness both check against it), and it costs users nothing -- no import-time
work, no runtime cost unless called.
"""

from __future__ import annotations

from chronotrace.reconstruct._replay import (
    apply_deltas,
    empty_work,
    freeze,
    overlay_events,
)
from chronotrace.reconstruct.types import ProgramState
from chronotrace.store import ChronoReader


def reconstruct_slow(reader: ChronoReader, seq: int) -> ProgramState:
    """The program state after event `seq`, replayed from the very beginning.

    Complexity: **O(seq)** -- deliberately. It touches every delta and event from 0, so
    it never consults a keyframe and cannot be wrong about which one to use.

    Raises:
        IndexError: `seq` is outside `[0, len(reader))`. Never clamped: clamping would
            invent a state the program was never in.
    """
    if not 0 <= seq < len(reader):
        raise IndexError(f"seq {seq} out of range [0, {len(reader)})")
    work = empty_work()
    apply_deltas(work, reader.deltas_between(0, seq))
    overlay_events(work, reader[0 : seq + 1])  # type: ignore[arg-type]
    return freeze(work, seq, reader[seq].frame_id)  # type: ignore[union-attr]
