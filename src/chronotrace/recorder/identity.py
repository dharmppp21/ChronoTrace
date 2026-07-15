"""Durable identity for recorded objects, without keeping them alive.

The UI wants to say "these two variables are the same object" -- that badge is how
a user spots an aliasing bug, which is exactly the bug class `examples/` is built
around. So a captured object needs an id that stays meaningful for the life of a
recording.

Why not `id()` alone
--------------------
`id()` is unique only among **live** objects; CPython reuses addresses. Day 3
reproduced a collision within 10,000 allocations. Used as a durable id, two
unrelated objects minutes apart would share an identity and the UI would draw
"same object" between things that never coexisted -- actively misleading on the
one bug class the badge exists to catch.

Why not `WeakKeyDictionary`, against the day-7 brief
----------------------------------------------------
The brief said to store ids in a `WeakKeyDictionary`. Measured, it is the wrong
tool, twice over:

* **It calls user code.** `wkd[obj]` hashes the key, so a user's `__hash__` and
  `__eq__` run -- breaking the invariant capture.py exists to enforce. A
  `__hash__` with a side effect would make the debugger cause the bug it is
  hunting.
* **It is value-keyed, not identity-keyed.** Two *distinct* objects that compare
  equal map to one entry, so they would share an id. That is the precise opposite
  of what an identity map means, and it would make the aliasing badge lie.

It also requires hashable keys, so a `set` -- weakref-able but unhashable -- gets
nothing.

What this uses instead: `id()` keys, weakref callbacks for liveness. `id(obj)` is
unique while `obj` lives; a `weakref.ref(obj, callback)` fires the moment it dies,
dropping the entry before the address can be reused. Identity-keyed, weak, and it
touches no user code -- `id()` and `weakref.ref()` are both C-level and cannot be
overridden.

The gap this leaves, stated plainly
-----------------------------------
**`dict`, `list` and `tuple` cannot hold a weak reference** (measured), so they get
no durable identity. That is not a footnote: `examples/buggy_pipeline.py` -- the
demo bug the project is built around -- is a **dict** mutated through an alias,
exactly the case the badge exists to reveal. Custom classes, sets and functions do
get ids, so the mechanism is real; it does not yet reach the most important case.

Three options exist and none is chosen today, because day 37 is the only consumer
and inventing a scheme for a caller that does not exist is guessing:

* Store raw `id()` and let the UI compare it **only within one instant** -- sound,
  since objects captured microseconds apart are all alive, but subtle and easy for
  a later reader to misuse across instants.
* Wrap containers in a weakref-able proxy at capture time -- an allocation per
  container, in the hot path.
* Accept the gap: badges on custom objects only.

Returning `None` is the honest answer until day 37 decides: no identity is better
than a wrong one.
"""

from __future__ import annotations

import itertools
import weakref
from typing import Any


class _KeyedRef(weakref.ref[Any]):
    """A weak reference that remembers which key it was stored under.

    A weakref callback receives the *reference*, not the key, so without this the
    cleanup would have to scan every stored ref to find the dead one -- O(n) per
    object death, O(n**2) across a recording. The stdlib solves this the same way
    inside `WeakValueDictionary`.
    """

    __slots__ = ("key",)

    key: int

    def __new__(cls, obj: object, callback: Any, key: int) -> _KeyedRef:
        ref: _KeyedRef = super().__new__(cls, obj, callback)
        ref.key = key
        return ref

    def __init__(self, obj: object, callback: Any, key: int) -> None:
        super().__init__(obj, callback)  # type: ignore[call-arg]


class ObjectIdentity:
    """Hands out stable ids to objects, holding none of them.

    Not thread-safe by locking. A race can only cause two threads to assign two
    ids to one object -- the UI would miss a badge it could have drawn, which is a
    cosmetic loss, never a wrong answer. A lock here would cost every captured
    object to prevent that.
    """

    __slots__ = ("_counter", "_ids", "_refs")

    def __init__(self) -> None:
        self._ids: dict[int, int] = {}
        self._refs: dict[int, _KeyedRef] = {}
        self._counter = itertools.count(1)  # 0 would read as "no identity"

    def of(self, obj: object) -> int | None:
        """The stable id for `obj`, assigning one on first sight.

        Args:
            obj: any object. Non-weakref-able values (int, str, tuple, dict, list)
                return None -- see the module docstring.

        Returns:
            A monotonic id, or None where identity is impossible or meaningless.

        Complexity: O(1) -- one dict probe on the hit path. Called once per
        captured container or object, never per atom.

        Touches no user code: `id()` and `weakref.ref()` are C-level and cannot be
        overridden, unlike the `__hash__`/`__eq__` a dict-keyed map would invoke.
        """
        key = id(obj)
        existing = self._ids.get(key)
        if existing is not None:
            return existing
        try:
            ref = _KeyedRef(obj, self._forget, key)
        except TypeError:
            # Not weakref-able. int/str/tuple are immutable, so "is it the same
            # object?" has no user-visible meaning for them anyway; dict/list are
            # the real loss, and are the tracked gap.
            return None
        new_id = next(self._counter)
        self._ids[key] = new_id
        self._refs[key] = ref  # keep the ref alive, or the callback never fires
        return new_id

    def _forget(self, ref: _KeyedRef) -> None:
        """Drop an object's entry the moment it dies.

        This is what makes `id()` safe as a key here: the entry cannot outlive the
        address, so a reused address always finds an empty slot and gets a fresh
        monotonic id.

        Complexity: O(1), via the key the ref carries.
        """
        self._refs.pop(ref.key, None)
        self._ids.pop(ref.key, None)
