"""Value references: the indirection that makes recording affordable.

An event never embeds a value. It carries a `ValueRef` -- an integer index into a
pool. This is not premature indirection; it is the mechanism that decides whether
the project is viable at all.

Day 3 measured it. Capturing every local on every line of a realistic workload
cost **2,370x** -- a 6.9ms program took 16.5 seconds -- because a 1200-element
list was re-walked on all 13,210 lines despite never changing after line one.
Capturing each distinct value once and referencing it thereafter took the same
workload to **6.1x** (`spikes/RESULTS-capture.md`).

So: a loop that touches the same list a million times captures it a million times
(a mutable list could change under us, so we cannot skip the capture -- see
dedup.py), but *stores* it once and hands back the same reference every time.

Deduplication is content-addressed and lives in `dedup.py`; this pool just owns
the storage and routes `add` through the cache. The *type* has been fixed since
day 4 because seven layers reference `ValueRef`; day 8 filled in the body.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NewType

from chronotrace.recorder.capture import CapturedValue
from chronotrace.recorder.dedup import DEFAULT_BUDGET_BYTES, DedupCache, digest

ValueRef = NewType("ValueRef", int)
"""Index into a `ValuePool`.

`NewType` rather than a bare `int`: it costs nothing at runtime and stops a
`frame_id` being passed where a `ValueRef` belongs -- both are ints, and every
field in the event model is an int, so the type checker is the only thing
standing between us and a silent field mix-up.
"""


class ValuePool:
    """Holds captured values once each; hands out references to them.

    Content-addressed: adding a representation already in the pool returns the
    existing reference instead of storing a copy. The dedup cache (`dedup.py`) is
    the accelerator; this pool is the source of truth and never evicts -- it *is*
    the recording. Cache eviction can only make a value be stored twice, never
    shown wrong.

    This pool stores **captured representations** -- plain nested dicts/lists
    from the capturer -- never the user's live objects. That is what keeps
    the recorder from extending a recorded object's lifetime, which would change
    when the program's finalisers run and could mask the very bug being hunted.
    """

    __slots__ = ("_cache", "_values")

    def __init__(self, budget_bytes: int = DEFAULT_BUDGET_BYTES) -> None:
        """Build an empty pool.

        Args:
            budget_bytes: memory ceiling for the dedup *cache* (not the pool,
                which is unbounded because it is the recording). See
                `dedup.DEFAULT_BUDGET_BYTES`.
        """
        self._values: list[CapturedValue] = []
        self._cache = DedupCache(budget_bytes)

    def add(self, captured: CapturedValue) -> ValueRef:
        """Store a captured representation once and return its reference.

        Identical content added twice returns the same reference; that collapse
        is the entire reason recording is affordable (see the module docstring).

        Args:
            captured: plain data from the capturer, never a live user object.

        Returns:
            A reference valid for the lifetime of this pool. Equal content always
            maps to an equal reference.

        Complexity: O(size of the captured representation) for the content hash,
        which the capture policy bounds; O(1) storage.
        """
        key = digest(captured)
        seen = self._cache.get(key)
        if seen is not None:
            return seen
        ref = ValueRef(len(self._values))
        self._values.append(captured)
        self._cache.put(key, ref)
        return ref

    def __iter__(self) -> Iterator[CapturedValue]:
        """Every captured value, in reference order -- so `enumerate` gives back the refs.

        Exists so a writer can persist the pool without reaching into it: the order *is*
        the reference numbering, and any other order would break every event's `value_ref`.
        """
        return iter(self._values)

    def resolve(self, ref: ValueRef) -> CapturedValue:
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
