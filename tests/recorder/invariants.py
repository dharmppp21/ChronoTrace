"""Frame invariants that must hold for every recording, forever.

Not a test file -- a library of assertions that test files call. It lives here
rather than in a conftest fixture because later phases assert the same things
about recordings that come from a *file* rather than a `MemorySink`, and a fixture
would tie them to today's plumbing.

The central invariant, `assert_frames_balanced`, is the one day 6 exists to make
true and every later phase depends on: the day-27 call-tree index stores
`entry_seq`/`exit_seq` per frame, and a frame that never exits has no `exit_seq`,
so every query with a range predicate silently loses it.
"""

from __future__ import annotations

from collections.abc import Iterable

from chronotrace.recorder import Event, EventKind

DEATHS = frozenset({EventKind.RETURN, EventKind.UNWIND})
"""A frame is gone for good."""


def assert_frame_lifecycles_are_well_formed(events: Iterable[Event]) -> None:
    """Each frame's own event sequence is a legal life story.

    Per frame_id, in seq order, the shape must be::

        CALL  (YIELD RESUME)*  (RETURN | UNWIND)

    with LINE and exception events allowed anywhere in between. Checked per frame
    rather than as a global depth counter, and that distinction is the whole lesson
    of day 6.

    A global counter -- push on CALL/RESUME, pop on YIELD/RETURN/UNWIND -- is a
    stack wearing a different hat, and it fails on the same case the stack model
    failed on: a generator collected while *suspended* unwinds without ever
    re-entering, so its UNWIND has no matching entry and the depth goes negative.
    That is correct behaviour being wrongly asserted. The first version of this
    file made exactly the mistake the frame model had just been rewritten to stop
    making.

    Per-frame checking is also *independent of the implementation*: it does not
    replicate the registry's rules, it describes what a frame's life must look
    like from the outside. A test that mirrors the code it tests proves only that
    the code agrees with itself.

    Args:
        events: a recording, in seq order.

    Raises:
        AssertionError: naming the frame and the illegal transition.

    Complexity: O(n).
    """
    state: dict[int, str] = {}  # frame_id -> "running" | "suspended" | "dead"
    for event in events:
        frame_id = event.frame_id
        if frame_id == 0:  # NO_FRAME: recording began mid-execution
            continue
        current = state.get(frame_id)

        if event.kind is EventKind.CALL:
            assert current is None, f"frame {frame_id} called while {current}"
            state[frame_id] = "running"
        elif event.kind is EventKind.YIELD:
            assert current == "running", f"frame {frame_id} yielded while {current}"
            state[frame_id] = "suspended"
        elif event.kind is EventKind.RESUME:
            assert current == "suspended", f"frame {frame_id} resumed while {current}"
            state[frame_id] = "running"
        elif event.kind in DEATHS:
            # UNWIND may kill a suspended frame (GeneratorExit) or a running one.
            assert current in ("running", "suspended"), f"frame {frame_id} died while {current}"
            state[frame_id] = "dead"
        else:
            assert current != "dead", f"frame {frame_id} emitted {event.kind.name} after dying"

    alive = {f: st for f, st in state.items() if st != "dead"}
    assert alive == {}, f"frames never died: {alive}"


def assert_every_frame_dies_once(events: Iterable[Event]) -> None:
    """Every frame born (CALL) dies exactly once (RETURN or UNWIND).

    The invariant that proves the registry cannot leak. An abandoned generator
    must still die -- via GeneratorExit -- and a frame must never die twice.

    Args:
        events: a recording, in seq order.

    Raises:
        AssertionError: naming the offending frame_id.

    Complexity: O(n).
    """
    born: set[int] = set()
    died: set[int] = set()
    for event in events:
        if event.kind is EventKind.CALL:
            assert event.frame_id not in born, f"frame {event.frame_id} born twice"
            born.add(event.frame_id)
        elif event.kind in DEATHS:
            assert event.frame_id not in died, f"frame {event.frame_id} died twice"
            died.add(event.frame_id)
    assert born - died == set(), f"frames born but never died: {sorted(born - died)}"


def assert_seq_is_a_total_order(events: Iterable[Event]) -> None:
    """seq strictly increases across the whole recording.

    Interleaved coroutines mean no per-frame structure can answer "what happened
    next". Only the global clock can, so it must never tie or go backwards.

    Args:
        events: a recording.

    Raises:
        AssertionError: at the first non-increasing pair.

    Complexity: O(n).
    """
    previous = -1
    for event in events:
        assert event.seq > previous, f"seq {event.seq} did not follow {previous}"
        previous = event.seq
