"""Tracks which frames are alive and which one is executing.

Day 5 used a stack. **The stack was wrong**, and this file exists because reality
said so rather than because a design got fancier.

The counter-example that killed it
----------------------------------
Two generators of the same function, interleaved. Measured event sequence::

    START  gen  F0        # first generator begins
    YIELD  gen  F0        # ...and leaves without returning
    START  gen  F1        # second generator begins
    YIELD  gen  F1
    RESUME gen  F0        # F0 comes BACK -- after F1 started
    YIELD  gen  F0
    RESUME gen  F1
    YIELD  gen  F1

A stack says a frame is entered once, exited once, last-in-first-out. F0 leaves,
F1 enters, F0 re-enters. That is not a stack in any sense; forcing one on it means
either inventing a new `frame_id` on every `RESUME` (so a generator becomes N
unrelated frames and the call tree is a lie) or special-casing until the stack is
a registry wearing a stack's name.

Under `asyncio.gather` the same thing happens with coroutines -- which are
generators underneath -- except many are suspended at once. This is precisely why
`seq` is a global clock: interleaving means "what happened next" cannot be
answered by any per-frame structure, only by a total order over all events.

The model that matches reality
------------------------------
* A **registry** of live frames: `id(frame) -> frame_id`. A frame is live from
  `PY_START` until `PY_RETURN`/`PY_UNWIND`. `PY_YIELD` does *not* end a frame; it
  suspends one.
* A **stack of what is currently executing**, per thread. `PY_START` and
  `PY_RESUME` push; `PY_YIELD`, `PY_RETURN` and `PY_UNWIND` pop. This is
  well-nested even when frame *lifetimes* are not, which is why depth stays sane
  while identity needs the registry.

So: entries = START + RESUME, exits = YIELD + RETURN + UNWIND. They balance.

Why `id(frame)` is safe here specifically
-----------------------------------------
Day 3 proved `id()` is unique only among *live* objects and CPython reuses
addresses -- which is why `frame_id` is a monotonic counter and never an id. The
registry's key is different: it maps an address only while that frame is alive,
and drops it the instant the frame exits.

That is *almost* enough, and the gap is worth stating because it bit. It assumes
every frame that enters also exits, so a reused address always finds an empty
slot. When an entry leaks -- CPython < 3.13 emits no `PY_UNWIND` for a generator
finalised by the collector, see issue #4 -- the assumption fails, the address is
reused while the stale entry is still there, and `enter` hands a brand-new frame
the dead one's id. Two unrelated frames fuse into one, which is precisely what
`frame_id` exists to prevent.

`enter(..., resuming=)` closes it: a `PY_START` never recovers an id, because a
frame that is starting did not exist a moment ago. Only `PY_RESUME` recovers.
Found by the day-22 equivalence harness on CPython 3.12.

Frames are **not** weakref-able (checked), so a `WeakKeyDictionary` was not an
option. We store `id(frame)` -- an int -- never the frame itself, so the registry
cannot pin the user's locals alive.

Abandoned generators
--------------------
On CPython >= 3.13 a generator dropped without being exhausted still exits:
`GeneratorExit` is thrown into it during collection, producing
`RAISE -> EXCEPTION_HANDLED -> RERAISE -> PY_UNWIND`, and the `UNWIND` removes it.
On 3.12 that `PY_UNWIND` is not emitted, so the entry leaks -- an interpreter
limitation tracked as issue #4, and the reason the paragraph above exists.
"""

from __future__ import annotations

import itertools
import threading
from types import FrameType

NO_FRAME = 0
"""Frame id for events whose frame we never saw start.

Recording can begin mid-execution, so events arrive for frames already running.
They are real history with unknown parentage; dropping them to protect a
bookkeeping invariant would lose more than it saves.
"""


class _ThreadStack(threading.local):
    """What is executing *right now*, per thread.

    Thread-local because `sys.monitoring.set_events` is process-global: callbacks
    fire on every thread, and one shared stack would interleave threads into
    nonsense. The registry itself is deliberately *not* thread-local -- a
    generator created on one thread can legally be resumed on another, and a
    per-thread registry would hand it a second identity.
    """

    def __init__(self) -> None:
        self.executing: list[int] = []


class FrameRegistry:
    """Live frames, and which one is executing.

    Complexity: every operation is O(1) -- one dict probe plus one list push or
    pop. This runs on frame lifecycle events (thousands) rather than LINE events
    (millions), so the dict probe is affordable where it would not be in the
    LINE path.
    """

    __slots__ = ("_counter", "_live", "_stack")

    def __init__(self) -> None:
        self._live: dict[int, int] = {}
        self._counter = itertools.count(1)  # 0 is NO_FRAME
        self._stack = _ThreadStack()

    @property
    def current(self) -> int:
        """The frame_id executing on this thread, or NO_FRAME.

        Read by LINE events, which is the hot path -- hence a list index rather
        than anything cleverer.
        """
        executing = self._stack.executing
        return executing[-1] if executing else NO_FRAME

    @property
    def live_count(self) -> int:
        """How many frames are alive. Zero at the end of a balanced program."""
        return len(self._live)

    def enter(self, frame: FrameType, *, resuming: bool = False) -> int:
        """A frame started or resumed. Returns its frame_id.

        The same `frame_id` is returned across a generator's whole life: `PY_START`
        assigns one, and every `PY_RESUME` recovers it. Assigning a fresh id on
        resume would split one generator into N unrelated frames, and the day 27
        call tree would show a program that never ran.

        `resuming` is what makes that recovery safe. A `PY_START` is by definition a
        frame that did not exist a moment ago, so finding its address already mapped
        proves the previous owner died **without an exit event** and CPython reused the
        address. Recovering that id would fuse two unrelated frames into one -- the exact
        failure `frame_id` exists to prevent, arriving through the back door. So a start
        always takes a fresh id; only a resume recovers.

        Args:
            frame: the frame entering. Only its `id()` is kept, never a reference.
            resuming: True for `PY_RESUME`, False for `PY_START`.

        Returns:
            The frame's stable id.

        Complexity: O(1).
        """
        key = id(frame)
        frame_id = self._live.get(key)
        if frame_id is None or not resuming:
            frame_id = next(self._counter)
            self._live[key] = frame_id  # replaces a stale entry, if the address was reused
        self._stack.executing.append(frame_id)
        return frame_id

    def id_of(self, frame: FrameType) -> int:
        """The frame_id for `frame`, or NO_FRAME if we never saw it start.

        Read-only. Used by events that report *about* a frame without changing
        whether it executes -- an exception surfacing, for instance.

        Complexity: O(1).
        """
        return self._live.get(id(frame), NO_FRAME)

    def suspend(self, frame: FrameType) -> int:
        """A frame yielded. It stops executing but stays alive.

        The distinction from `exit` is the whole point of the registry: a
        suspended generator is still a live frame with live locals, and a user
        scrubbing the timeline expects to see it.

        Args:
            frame: the frame suspending.

        Returns:
            The suspended frame_id, or NO_FRAME if we never saw it start.

        Complexity: O(1).
        """
        return self._stop_executing(self._live.get(id(frame), NO_FRAME))

    def exit(self, frame: FrameType) -> int:
        """A frame returned or unwound. It is gone.

        Args:
            frame: the frame leaving.

        Returns:
            The departed frame_id, or NO_FRAME if we never saw it start.

        Complexity: O(1).
        """
        return self._stop_executing(self._live.pop(id(frame), NO_FRAME))

    def _stop_executing(self, frame_id: int) -> int:
        """Take `frame_id` off the executing stack -- but only if it is on top.

        The guard is not defensive padding; it is the fix for a real bug. A
        generator collected while suspended unwinds (GeneratorExit) *without ever
        executing*, so the stack's top belongs to somebody else. Popping blindly
        stole the caller's frame, and every subsequent event in that caller
        reported the wrong frame_id -- silently, since the depth still balanced.

        The frame_id must therefore come from the registry, which knows the frame's
        identity, rather than from the stack, which only knows what is running.
        """
        executing = self._stack.executing
        if executing and executing[-1] == frame_id:
            executing.pop()
        return frame_id
