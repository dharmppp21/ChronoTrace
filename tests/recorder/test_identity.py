"""Pins the identity contract, including the traps that shaped it."""

from __future__ import annotations

import gc
import weakref

from chronotrace.recorder.identity import ObjectIdentity


class Tracked:
    """A plain weakref-able, identity-hashed object -- the case that works."""


def test_same_object_gets_the_same_id() -> None:
    identity = ObjectIdentity()
    obj = Tracked()
    assert identity.of(obj) == identity.of(obj)


def test_distinct_objects_get_distinct_ids() -> None:
    identity = ObjectIdentity()
    assert identity.of(Tracked()) != identity.of(Tracked())


def test_ids_are_monotonic_from_one() -> None:
    """0 is reserved: it would read as "no identity" in the captured data."""
    identity = ObjectIdentity()
    a, b = Tracked(), Tracked()
    assert identity.of(a) == 1
    assert identity.of(b) == 2


def test_never_calls_user_hash_or_eq() -> None:
    """The trap that killed the WeakKeyDictionary the brief asked for.

    `wkd[obj]` hashes the key, running user `__hash__`/`__eq__` -- which breaks
    capture's no-user-code invariant. Keying on `id()` cannot: it is C-level and
    unoverridable.
    """
    called: list[str] = []

    class Sneaky:
        def __hash__(self) -> int:
            called.append("__hash__")
            return 1

        def __eq__(self, other: object) -> bool:
            called.append("__eq__")
            return True

    identity = ObjectIdentity()
    identity.of(Sneaky())
    identity.of(Sneaky())
    assert called == [], f"identity invoked user code: {called}"


def test_equal_but_distinct_objects_get_distinct_ids() -> None:
    """The second WeakKeyDictionary trap: it is value-keyed, not identity-keyed.

    Two objects that compare equal are still two objects. A map that fused them
    would make the aliasing badge claim a relationship that does not exist -- the
    exact opposite of what identity means.
    """

    class AlwaysEqual:
        def __hash__(self) -> int:
            return 1

        def __eq__(self, other: object) -> bool:
            return True

    identity = ObjectIdentity()
    a, b = AlwaysEqual(), AlwaysEqual()
    assert identity.of(a) != identity.of(b)


def test_does_not_retain_the_object() -> None:
    """The invariant: the recorder never extends a recorded object's lifetime."""
    identity = ObjectIdentity()
    obj = Tracked()
    ref = weakref.ref(obj)
    identity.of(obj)

    del obj
    gc.collect()
    assert ref() is None, "identity retained the object"


def test_id_reuse_does_not_leak_an_identity_across_objects() -> None:
    """The whole reason the weakref callback exists.

    CPython reuses addresses. If a dead object's entry outlived it, the next object
    at that address would inherit its identity -- and the UI would draw "same
    object" between two things that never coexisted. The callback drops the entry
    before the address can be handed out again.
    """
    identity = ObjectIdentity()
    seen_ids: set[int] = set()
    raw_addresses: list[int] = []

    for _ in range(2_000):
        obj = Tracked()
        raw_addresses.append(id(obj))
        assigned = identity.of(obj)
        assert assigned is not None
        assert assigned not in seen_ids, "an identity was handed out twice"
        seen_ids.add(assigned)
        del obj
        gc.collect()

    assert len(set(raw_addresses)) < len(raw_addresses), (
        "sanity: CPython should have reused an address across 2000 allocations"
    )


def test_dead_entries_are_reclaimed() -> None:
    """The map must not grow with objects that no longer exist."""
    identity = ObjectIdentity()
    for _ in range(500):
        identity.of(Tracked())
    gc.collect()
    assert len(identity._ids) < 50, "dead entries were not reclaimed"


def test_non_weakrefable_values_get_no_identity() -> None:
    """Atoms and builtin containers. See the module docstring's tracked gap."""
    identity = ObjectIdentity()
    for value in (42, "hello", (1, 2), {"a": 1}, [1, 2]):
        assert identity.of(value) is None


def test_sets_do_get_identity() -> None:
    """Weakref-able but unhashable -- the case WeakKeyDictionary could not serve."""
    identity = ObjectIdentity()
    assert identity.of(set()) is not None
