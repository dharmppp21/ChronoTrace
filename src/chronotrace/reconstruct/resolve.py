"""Turn a `ValueRef` into the captured value -- lazily, and only the ones actually looked at.

Why lazy
--------
A `ProgramState` holds refs, not values, and that is the whole reason reconstruction is
cheap: a frame with 50 locals costs 50 integers to reconstruct. The user then looks at
two of them. Resolving all 50 eagerly would decode 48 values nobody asked for -- and,
because the day-14 pool is content-addressed and stored once, resolving one is a directory
lookup plus one msgpack decode. So resolution stays a separate step, driven by what the UI
actually renders, and the expensive part is never paid for the invisible.

Why an LRU on top
-----------------
Scrubbing re-renders the same handful of variables at every step of a drag, and a value
that did not change keeps the *same ref* (day-8 content addressing). So the same few refs
are resolved thousands of times in a row -- a small LRU turns that into a dict hit. The
bound is entry count, not bytes, because captured values are already bounded by the
day-7 capture policy (`max_nodes`), so an entry cannot be arbitrarily large.

A missing ref is an error, never a `None`
-----------------------------------------
If a ref is not in the pool the recording is corrupt or the ref came from somewhere it
should not have. Returning `None` would render as "the variable was None", which is a
*lie* about the program -- exactly the failure a debugger cannot commit. It raises.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping

from chronotrace.recorder.capture import CapturedValue
from chronotrace.store import ChronoError, ChronoReader

DEFAULT_CACHE_ENTRIES = 4096
"""Resolved values kept. Each is bounded by the capture policy (day 7), so the cache is
bounded by entry count alone -- a few thousand small structures."""


class MissingValue(ChronoError):
    """A `ValueRef` is not in the recording's value pool.

    A corrupt file, or a ref that never belonged to this recording. Raised rather than
    resolved to `None`, which would render as a real value and lie about the program.
    """


class ValueResolver:
    """Resolves `ValueRef` -> captured value through the pool, with an LRU.

    Holds a `ChronoReader` and nothing else; the pool itself is decoded once and cached
    inside the reader, so this adds only the resolved-object cache on top.
    """

    __slots__ = ("_cache", "_max_entries", "_reader")

    def __init__(self, reader: ChronoReader, *, max_entries: int = DEFAULT_CACHE_ENTRIES) -> None:
        self._reader = reader
        self._max_entries = max_entries
        self._cache: OrderedDict[int, CapturedValue] = OrderedDict()

    def resolve(self, ref: int) -> CapturedValue:
        """The captured value for `ref`.

        Raises:
            MissingValue: the ref is not in the pool.

        Complexity: O(1) on a cache hit; one pool directory lookup plus one msgpack
        decode on a miss.
        """
        cached = self._cache.get(ref)
        if cached is not None:
            self._cache.move_to_end(ref)
            return cached
        try:
            value = self._reader.value(ref)
        except IndexError as exc:
            raise MissingValue(f"value_ref {ref} is not in this recording's pool") from exc
        self._cache[ref] = value
        if len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
        return value

    def resolve_bindings(self, bindings: Mapping[int, int]) -> dict[int, CapturedValue]:
        """Resolve a whole frame's bindings -- for when the UI *does* render them all.

        Offered so callers never hand-roll the loop, not because eager resolution is the
        default: the point of `resolve` is that a caller can skip what it does not show.
        """
        return {name_id: self.resolve(ref) for name_id, ref in bindings.items()}
