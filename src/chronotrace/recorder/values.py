"""Value references: the indirection that makes recording affordable.

An event never embeds a value. It carries a `ValueRef` -- an integer index into a
pool. This is not premature indirection; it is the mechanism that decides whether
the project is viable at all.

Day 3 measured it. Capturing every local on every line of a realistic workload
cost **2,370x** -- a 6.9ms program took 16.5 seconds -- because a 1200-element
list was re-walked on all 13,210 lines despite never changing after line one.
Capturing each distinct value once and referencing it thereafter took the same
workload to **6.1x** (`spikes/RESULTS-capture.md`).

So: a loop that touches the same list a million times pays for one capture and
999,999 integer copies.

Today's pool is a placeholder -- it appends and returns an index, deduplicating
nothing. Day 8 makes it a content-addressed cache, which is where the 387x
actually comes from. The *type* is fixed today because seven layers reference it;
the *implementation* is deliberately trivial until there is something to measure.
"""

from __future__ import annotations

from typing import Any, NewType

ValueRef = NewType("ValueRef", int)
"""Index into a `ValuePool`.

`NewType` rather than a bare `int`: it costs nothing at runtime and stops a
`frame_id` being passed where a `ValueRef` belongs -- both are ints, and every
field in the event model is an int, so the type checker is the only thing
standing between us and a silent field mix-up.
"""


class ValuePool:
    """Holds captured values; hands out references to them.

    Placeholder implementation: no deduplication (day 8). The interface is what
    matters today, because `events.py` and every layer above it name `ValueRef`.

    This pool stores **captured representations** -- plain nested dicts/lists
    from day 3's capturer -- never the user's live objects. That is what keeps
    the recorder from extending a recorded object's lifetime, which would change
    when the program's finalisers run and could mask the very bug being hunted.
    """

    __slots__ = ("_values",)

    def __init__(self) -> None:
        self._values: list[Any] = []

    def add(self, captured: Any) -> ValueRef:
        """Store a captured representation and return its reference.

        Args:
            captured: plain data from the capturer, never a live user object.

        Returns:
            A reference valid for the lifetime of this pool.

        Complexity: O(1) amortised. Day 8 adds hashing, making it O(size of the
        captured representation) in exchange for deduplication.
        """
        self._values.append(captured)
        return ValueRef(len(self._values) - 1)

    def resolve(self, ref: ValueRef) -> Any:
        """Return the captured representation for `ref`.

        Args:
            ref: a reference previously returned by `add`.

        Returns:
            The captured representation.

        Raises:
            IndexError: `ref` was never issued by this pool.

        Complexity: O(1).
        """
        return self._values[ref]
