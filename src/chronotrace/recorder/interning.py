r"""Interning: turn repeated filenames and code objects into small integers.

A recording emits millions of events, and almost every one names a filename and a
function. `"D:\\proj\\src\\pipeline.py"` is ~24 bytes; repeated 750,000 times it
is 18 MB of the same string. The event model therefore carries `code_id: int` and
a side table maps ids back to what they name.

Two wins, and the second is the larger one:

* **Memory and wire size.** An int8-to-int64 id against a variable-length string,
  per event.
* **Compression.** This is why interning is a first-class subsystem rather than a
  later optimisation. Day 12 encodes events as columns and applies run-length and
  delta encoding. A column of small integers that stay constant for long stretches
  (a program stays in one function for many lines) compresses to almost nothing.
  A column of strings does not. Interning is what makes the storage format's
  central trick work, so it cannot be bolted on afterwards.

One table type, two instances (code objects and variable names). The generic is
earned by a second real caller today, not a hypothetical one.
"""

from __future__ import annotations

from collections.abc import Iterator


class InternTable[T]:
    """Assigns a stable small integer to each distinct value it sees.

    Ids are handed out densely from 0 in first-sight order, which is what makes
    them cheap to store: a recording touching 50 code objects uses ids 0-49, and
    day 12's columnar encoder can pack that into a byte per event.

    **On holding references.** This table keeps a strong reference to every key.
    For code objects that is deliberate and safe, and the distinction matters:
    code objects are program *structure*, bounded by the size of the source, so
    the table's size is bounded by the program's code and not by its data. The
    recorder's "never retain" invariant exists to stop us pinning the program's
    *data* alive -- which is unbounded and would change GC timing, potentially
    masking the refcount bug being debugged. Pinning a few hundred code objects
    does neither.

    Code objects are in fact weakref-able, so a `WeakKeyDictionary` was possible
    and was rejected: it costs speed in the hot path to solve one narrow case (a
    program `exec`-ing unboundedly many distinct code objects *during* a
    recording). Recordings are bounded in time; the case is speculative. Tracked
    for revisit only if a real program hits it.
    """

    __slots__ = ("_ids", "_values")

    def __init__(self) -> None:
        self._ids: dict[T, int] = {}
        self._values: list[T] = []

    def intern(self, value: T) -> int:
        """Return the id for `value`, assigning one on first sight.

        Args:
            value: anything hashable. Callers use code objects and strings.

        Returns:
            A dense id, stable for the lifetime of this table.

        Complexity: O(1) expected -- one hash, one dict probe. This runs on every
        recorded event, so the `.get`-then-branch shape is deliberate: it costs a
        single lookup on the hit path, where `if value not in self._ids` would
        cost two. Measured at ~71 ns for code objects, which hash by *value*
        (over their bytecode) rather than by identity -- see benchmarks/RESULTS.md.
        """
        existing = self._ids.get(value)
        if existing is not None:
            return existing
        new_id = len(self._values)
        self._ids[value] = new_id
        self._values.append(value)
        return new_id

    def __iter__(self) -> Iterator[T]:
        """Every interned value in id order, so `enumerate` reconstructs the id -> value map.

        Ids are dense from 0 (see the class docstring), which is what makes the position
        in this iteration *be* the id. Bulk resolution is what every layer above needs.
        """
        return iter(self._values)

    def resolve(self, id_: int) -> T:
        """Return the value an id names.

        Args:
            id_: an id previously returned by `intern`.

        Returns:
            The interned value.

        Raises:
            IndexError: `id_` was never issued by this table.

        Complexity: O(1). The parallel list exists for exactly this: dicts
        preserve insertion order, so ids could be resolved via `list(self._ids)`,
        but that is O(n) per lookup and the UI resolves constantly while
        scrubbing.
        """
        return self._values[id_]
