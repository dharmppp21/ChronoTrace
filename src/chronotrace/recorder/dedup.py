"""Content-addressed deduplication: the mechanism that makes recording affordable.

The correctness rule, stated before any implementation detail
-------------------------------------------------------------
**A value is deduplicated on its captured *content*, never on its object
identity.** Two captures collapse to one reference only when their serialised
representations are byte-for-byte identical. This is the whole safety argument,
so it comes first:

* A list mutated in place (`a.append(2)`) keeps its `id()` but changes its
  content, so it re-captures to different bytes and gets a *new* reference. The
  user is never shown a stale value -- which for a debugger is the one
  unforgivable failure, worse than being slow.
* Two distinct objects that happen to be equal capture to identical bytes and
  share one reference -- free deduplication, no `__eq__` call.

Why not an identity fast-path (the day-8 brief's recommended option (b))
-----------------------------------------------------------------------
The brief proposed skipping the re-capture for *immutable* objects by trusting
`id()`. Rejected, because a *sound* version buys nothing and the version that
would buy something is a correctness bug:

* Immutable **atoms** (`int`, `str`, `float`) capture for free -- `capture()`
  returns them unwrapped. There is no walk to skip, so an identity cache over
  them saves nothing.
* Immutable **containers** (`tuple`, large `frozenset`) are the only ones whose
  capture is expensive, and `id()` on them is *unsound* as a cache key: a
  non-immortal immutable can die and have its address reused by a different
  value between two line events (a `walrus := ...` inside a comprehension rebinds
  and frees within a single source line). The cache would then return the old
  value's reference for the new value -- a silent stale read, the exact bug this
  module exists to prevent.

So identity shortcuts are dropped entirely. Every local is re-captured every
line and deduplicated on content. That is more work per line than an unsound
`id()` shortcut, and it is always correct. The cost is bounded because
`capture()` is bounded (`max_nodes=512`), and the benchmark reports where it
lands rather than promising a number the sound design cannot hit.

Why hash the representation rather than key on it directly
----------------------------------------------------------
A `dict[bytes, ValueRef]` keyed on the raw serialised bytes would be
collision-proof, but the keys would be as large as the values (hundreds of bytes
each), so a byte-bounded cache would hold far fewer of them and its hit rate
would suffer. Hashing to a fixed 16 bytes lets the same memory budget hold ~10x
more entries, which is what a cache is *for*. The price is a collision
probability, sized below to be unreachable.

Why 128 bits, with the collision math
-------------------------------------
On a collision two different values share a reference, and the user is shown the
*wrong value* -- silent, and indistinguishable from a real recording. So the hash
must be wide enough that a collision cannot happen across any recording anyone
will ever make. Birthday bound, `n` distinct values, `b`-bit hash:

    P(collision) ~= n**2 / 2**(b+1)

At a deliberately extreme `n = 10**7` distinct values:

    64-bit :  1e14 / 3.7e19  ~= 2.7e-6   (~1 in 370,000 recordings -- too high)
    128-bit:  1e14 / 6.8e38  ~= 1.5e-25  (below the hardware's own error rate)

128 bits it is. `hashlib.blake2b(digest_size=16)` -- stdlib (the project ships
zero runtime dependencies, so `xxhash` is out), faster than sha256, and lets us
ask for exactly 16 bytes. `hash()` was never a candidate: it is 64-bit, salted
per process (so recordings would not be comparable), and defined by the user's
`__hash__` for their types -- user code, which day 7 banned from the hot path.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chronotrace.recorder.capture import CapturedValue
    from chronotrace.recorder.values import ValueRef

_ENTRY_BYTES = 128
"""Estimated retained bytes per cache entry: a 16-byte digest, an int reference,
and the OrderedDict node that links them. An estimate, not a measurement, because
the accounting only needs to be right to an order of magnitude to bound the cache
-- getsizeof would tie the budget to a CPython version for no real gain."""

DEFAULT_BUDGET_BYTES = 64 * 1024 * 1024
"""64 MiB, so ~512k distinct recent values (64 MiB / 128 B). All four Day-2
workloads have working sets far below this (see benchmarks/RESULTS.md), so the
default never evicts at benchmark scale -- eviction is a guard against pathological
long recordings, not a normal-case throttle. It is a ceiling, reached only by a
recording with half a million *distinct* values live in the window; a normal
recording holds a few thousand."""


def digest(captured: CapturedValue) -> bytes:
    """A 128-bit content address for one captured value.

    `repr` rather than `json.dumps`: it is deterministic for our closed output
    type set (dicts, lists, and the atoms `capture()` emits), needs no encoder
    for `complex` (which json cannot serialise), and encodes the type in the
    syntax -- so `repr(True) != repr(1) != repr(1.0)`, giving the type-tag
    distinctions the brief requires for free. It touches no user code: `capture()`
    only ever emits builtins, whose `repr` is C-level.

    Args:
        captured: plain data from `capture()` -- nested dicts/lists/atoms.

    Returns:
        16 raw bytes. Equal content yields equal bytes; see the module docstring
        for why distinct content practically never does.

    Complexity: O(nodes in `captured`), which `capture()` bounds at `max_nodes`.
    """
    return hashlib.blake2b(repr(captured).encode("utf-8", "surrogatepass"), digest_size=16).digest()


class DedupCache:
    """An LRU map from content digest to `ValueRef`, bounded by a byte budget.

    An accelerator, never a source of truth. The value pool keeps every distinct
    value; this cache only remembers *where* recently-seen content was stored so a
    repeat need not be stored again. Evicting an entry therefore costs a little
    storage -- the next sighting of that content is stored a second time in the
    pool -- and never costs correctness. A cache miss is always safe.

    Not thread-safe. The recorder holds one per recording and the LINE callback is
    the only writer; cross-thread recording shares a `seq` clock, not this cache
    (see recorder.py). A lock here would tax every captured value to prevent a
    duplicate pool entry, which eviction already tolerates.
    """

    __slots__ = ("_budget", "_map")

    def __init__(self, budget_bytes: int = DEFAULT_BUDGET_BYTES) -> None:
        """Build an empty cache.

        Args:
            budget_bytes: soft ceiling on retained memory. The oldest entries are
                evicted once the estimate exceeds it. See `DEFAULT_BUDGET_BYTES`.
        """
        self._map: OrderedDict[bytes, ValueRef] = OrderedDict()
        self._budget = budget_bytes

    def get(self, key: bytes) -> ValueRef | None:
        """The reference stored for `key`, or None on a miss. Marks it recent.

        Complexity: O(1).
        """
        ref = self._map.get(key)
        if ref is not None:
            self._map.move_to_end(key)
        return ref

    def put(self, key: bytes, ref: ValueRef) -> None:
        """Record that `key`'s content lives at `ref`, evicting LRU if over budget.

        Only ever called on a cache miss (`ValuePool.add` checks `get` first), so
        `key` is always new and the assignment already appends it at the tail --
        no `move_to_end` needed.

        Eviction keeps at least one entry: a single value larger than the whole
        budget still caches (and is simply the next to go), so `put` never loops
        forever on a budget smaller than one entry.

        Complexity: O(evicted), amortised O(1).
        """
        self._map[key] = ref
        while len(self._map) * _ENTRY_BYTES > self._budget and len(self._map) > 1:
            self._map.popitem(last=False)

    @property
    def nbytes(self) -> int:
        """Estimated retained bytes -- what the budget bounds."""
        return len(self._map) * _ENTRY_BYTES

    def __len__(self) -> int:
        return len(self._map)
