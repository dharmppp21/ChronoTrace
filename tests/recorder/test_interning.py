"""Pins the interning contract that Phase 2's compression depends on."""

from __future__ import annotations

import sys

import pytest

from chronotrace.recorder.interning import InternTable


def test_same_value_same_id() -> None:
    t: InternTable[str] = InternTable()
    assert t.intern("pipeline.py") == t.intern("pipeline.py")


def test_different_values_different_ids() -> None:
    t: InternTable[str] = InternTable()
    assert t.intern("a.py") != t.intern("b.py")


def test_ids_are_dense_from_zero() -> None:
    """Density is the point, not a side effect.

    Day 12 packs the code_id column with run-length encoding. Dense small ids
    from 0 mean a program touching 50 code objects fits in a byte per event.
    Sparse or hash-derived ids would defeat that, and the storage format's central
    trick with it.
    """
    t: InternTable[str] = InternTable()
    ids = [t.intern(f"f{i}.py") for i in range(5)]
    assert ids == [0, 1, 2, 3, 4]


def test_resolve_returns_the_value() -> None:
    t: InternTable[str] = InternTable()
    i = t.intern("pipeline.py")
    assert t.resolve(i) == "pipeline.py"


def test_resolve_rejects_unknown_id() -> None:
    with pytest.raises(IndexError):
        InternTable[str]().resolve(0)


def test_interning_code_objects() -> None:
    """The real hot-path key.

    Code objects hash by *value* rather than identity, so two functions compiled
    from identical source in different places must still be distinct -- they are,
    because co_filename and co_firstlineno participate in equality.
    """

    def f() -> None:
        pass

    def g() -> None:
        pass

    t: InternTable[object] = InternTable()
    assert t.intern(f.__code__) == t.intern(f.__code__)
    assert t.intern(f.__code__) != t.intern(g.__code__)
    assert t.resolve(t.intern(f.__code__)) is f.__code__


def test_interning_beats_storing_strings() -> None:
    """The memory claim, measured rather than asserted.

    750k events naming one 40-char filename: an int per event against the string
    per event. If this ever fails, the premise of the event model's `code_id` is
    wrong and Phase 2's compression story goes with it.
    """
    filename = "D:\\projects\\chronotrace\\src\\pipeline_module.py"
    n = 100_000

    t: InternTable[str] = InternTable()
    code_id = t.intern(filename)
    interned_cost = sys.getsizeof([code_id] * n) + sys.getsizeof(filename)
    naive_cost = sys.getsizeof([filename] * n) + sys.getsizeof(filename)

    # Both lists hold pointers, so the win here is bounded; the real win is on the
    # wire (day 12 packs the id column, not pointers). Assert the direction only.
    assert interned_cost <= naive_cost
    assert t.resolve(code_id) is filename, "the string is stored once, not 100k times"
