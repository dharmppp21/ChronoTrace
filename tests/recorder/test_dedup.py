"""Deduplication, and the one bug it exists to prevent: showing a stale value.

The mutation test is the centre of gravity. Everything else guards a corner of
the canonicalisation, the cache bound, or the recorder's change detection, but if
`test_in_place_mutation_gets_a_new_ref` ever passes wrongly the debugger is
lying, which is the worst thing it can do.
"""

from __future__ import annotations

from typing import Any

from chronotrace.recorder import EventKind, MemorySink, Recorder
from chronotrace.recorder.capture import capture
from chronotrace.recorder.dedup import DedupCache, digest
from chronotrace.recorder.values import ValuePool, ValueRef
from tests.fixtures import hostile


def _add(pool: ValuePool, obj: object) -> ValueRef:
    """Capture then store -- exactly what the recorder does per local."""
    return pool.add(capture(obj))


# ---------------------------------------------------------------------------
# The mutation problem -- the reason identity shortcuts were rejected
# ---------------------------------------------------------------------------


def test_in_place_mutation_gets_a_new_ref() -> None:
    """A list mutated in place keeps its id() but must get a new reference.

    This is *the* test. An identity shortcut that trusted id() for this list --
    the design the day-8 brief warned against -- would return the first
    reference for the mutated list, and the user would scrub to this line and see
    `[1]` where the program held `[1, 2]`. Content addressing makes that
    impossible: different content, different bytes, different reference.
    """
    a = [1]
    r1 = _add(pool := ValuePool(), a)
    a.append(2)
    r2 = _add(pool, a)

    assert r1 != r2, "in-place mutation was missed -- stale value bug"
    assert pool.resolve(r1)["items"] == [1]
    assert pool.resolve(r2)["items"] == [1, 2]


def test_identical_distinct_objects_share_one_ref() -> None:
    """Two separate but equal dicts dedup to one reference and one stored copy."""
    pool = ValuePool()
    r1 = _add(pool, {"id": 1, "name": "widget"})
    r2 = _add(pool, {"id": 1, "name": "widget"})  # a different object, equal content

    assert r1 == r2
    assert pool.resolve(r1) == pool.resolve(r2)


# ---------------------------------------------------------------------------
# Canonicalisation traps: things Python calls "equal" that are not one value
# ---------------------------------------------------------------------------


def test_true_and_one_and_one_point_zero_are_distinct() -> None:
    """`True == 1 == 1.0` in Python, but they are three different recorded values.

    The representation carries the type in its syntax (`repr(True)` != `repr(1)`),
    so hashing it keeps them apart -- unlike Python's own `hash`, under which all
    three collide.
    """
    pool = ValuePool()
    refs = {_add(pool, True), _add(pool, 1), _add(pool, 1.0)}
    assert len(refs) == 3


def test_negative_zero_is_distinct_from_zero() -> None:
    """-0.0 == 0.0, but they behave differently (1/-0.0 is -inf) so we keep them apart."""
    pool = ValuePool()
    assert _add(pool, -0.0) != _add(pool, 0.0)


def test_two_nans_dedup_to_one_ref() -> None:
    """NaN != NaN, yet both capture to the representation 'nan' and are one value.

    Deduping on `==` would either never collapse them or raise; deduping on the
    representation collapses them correctly and never touches `__eq__`.
    """
    pool = ValuePool()
    assert _add(pool, float("nan")) == _add(pool, float("nan"))


def test_interned_scalars_dedup_for_free() -> None:
    """The same small int or string, added twice, is stored once."""
    pool = ValuePool()
    assert _add(pool, 5) == _add(pool, 5)
    assert _add(pool, "hello") == _add(pool, "hello")
    assert len(pool._values) == 2  # 5 and "hello", each once


# ---------------------------------------------------------------------------
# The digest itself
# ---------------------------------------------------------------------------


def test_digest_is_stable_and_128_bit() -> None:
    value = capture({"a": [1, 2, 3]})
    assert digest(value) == digest(value)
    assert len(digest(value)) == 16  # 128 bits -- see dedup.py collision math


def test_digest_separates_type_tagged_atoms() -> None:
    assert digest(capture(True)) != digest(capture(1)) != digest(capture(1.0))


# ---------------------------------------------------------------------------
# The bounded cache: an accelerator, never a source of truth
# ---------------------------------------------------------------------------


def test_cache_respects_its_byte_budget() -> None:
    """Under a stream far larger than the budget, the cache stays bounded."""
    tiny = DedupCache(budget_bytes=64 * 10)  # room for ~10 entries at 128 B est.
    for i in range(100_000):
        cache_key = i.to_bytes(16, "little")
        tiny.put(cache_key, ValueRef(i))
    assert tiny.nbytes <= 64 * 10
    assert len(tiny) <= 10


def test_hit_on_evicted_entry_is_correct_just_slower() -> None:
    """Evicting a cache entry costs a duplicate pool copy, never a wrong value.

    Store A, flood the cache until A is evicted, then add A again. The pool now
    holds A twice under two references -- wasteful, but both resolve to A. This is
    the eviction contract: the cache accelerates, the pool is truth.
    """
    pool = ValuePool(budget_bytes=64 * 4)  # room for ~4 cache entries
    first = _add(pool, "A")
    for i in range(1000):
        _add(pool, i)  # distinct content, evicts "A" from the cache
    second = _add(pool, "A")

    assert first != second, "expected a duplicate pool entry after eviction"
    assert pool.resolve(first) == pool.resolve(second) == "A"


# ---------------------------------------------------------------------------
# Day 7's hostile zoo still passes through, now via the deduping path
# ---------------------------------------------------------------------------


def test_hostile_fixtures_dedup_without_raising_or_touching_user_code() -> None:
    """Every hostile value captures, hashes and stores, invoking no user code."""
    hostile.reset_sentinels()
    pool = ValuePool()
    for name, value in hostile.build_zoo().items():
        if name.startswith("_"):
            continue
        ref = _add(pool, value)
        assert isinstance(ref, int), name
        assert pool.resolve(ref) is not None or value is None, name
    assert not hostile.EXPLODED, "a __repr__ ran during capture or hashing"
    assert not hostile.TOUCHED, "user attribute access ran during capture or hashing"


# ---------------------------------------------------------------------------
# Recorder-level change detection: VAR_WRITE fires only on real change
# ---------------------------------------------------------------------------


def _record(fn: Any) -> Recorder:
    sink = MemorySink()
    rec = Recorder(sink, capture_values=True)
    with rec:
        fn()
    return rec


def _writes_for(rec: Recorder, name: str) -> list[ValueRef]:
    name_id = rec._names.intern(name)
    return [
        e.value_ref
        for e in rec.sink.events  # type: ignore[attr-defined]
        if e.kind is EventKind.VAR_WRITE and e.name_id == name_id and e.value_ref is not None
    ]


def test_unchanged_local_emits_exactly_one_var_write() -> None:
    """A constant read every iteration is recorded once, not once per line."""

    def loop() -> int:
        k = 42
        total = 0
        for _ in range(5):
            total += k
        return total

    rec = _record(loop)
    assert len(_writes_for(rec, "k")) == 1, "unchanged binding re-emitted"
    assert len(_writes_for(rec, "total")) > 1, "a binding that changes must re-emit"


def test_in_place_mutation_is_never_missed_end_to_end() -> None:
    """The mutation test at the recorder level: appends produce distinct writes.

    `acc` is one list object mutated across three lines. Change detection must
    emit a new VAR_WRITE each time because the *content* changed, even though the
    object's id() never did.
    """

    def build() -> list[int]:
        acc: list[int] = []
        acc.append(1)
        acc.append(2)
        return acc

    rec = _record(build)
    refs = _writes_for(rec, "acc")
    seen = [rec._values.resolve(r)["items"] for r in refs]
    assert seen == [[], [1], [1, 2]], f"a mutation was missed or merged: {seen}"
