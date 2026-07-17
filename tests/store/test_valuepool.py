"""The value pool: write-once by content, both refs resolve, collisions fail loud, and
a hostile directory cannot read out of bounds."""

from __future__ import annotations

import struct
from typing import Any

import pytest

from chronotrace.recorder.capture import capture
from chronotrace.store.valuepool import (
    MAX_VALUES,
    PoolCollision,
    ValuePoolWriter,
    decode_pool,
    unpack_value,
)


def _round_trip(writer: ValuePoolWriter) -> list[Any]:
    blobs = decode_pool(writer.encode())
    return [unpack_value(b) for b in blobs]


def test_distinct_values_round_trip_by_ref() -> None:
    pool = ValuePoolWriter()
    values = [capture(v) for v in ({"a": 1}, [1, 2, 3], "hello", 42, 3.5, {"a": 2})]
    refs = [pool.add(v) for v in values]
    assert refs == [0, 1, 2, 3, 4, 5]
    resolved = _round_trip(pool)
    for ref, original in zip(refs, values, strict=True):
        assert resolved[ref] == original


def test_same_value_twice_is_written_once_both_refs_resolve() -> None:
    pool = ValuePoolWriter()
    r1 = pool.add(capture({"region": "north", "sales": 100}))
    r2 = pool.add(capture({"region": "north", "sales": 100}))  # identical content
    r3 = pool.add(capture({"region": "south", "sales": 100}))
    assert r1 == r2  # same ref: stored once
    assert r3 != r1
    assert len(pool) == 2  # only two distinct values physically stored
    resolved = _round_trip(pool)
    assert resolved[r1] == resolved[r2] == capture({"region": "north", "sales": 100})


def test_collision_detection_fires_when_forced(monkeypatch: Any) -> None:
    """Force two DIFFERENT values to the same content hash: the pool must refuse the
    second rather than let a ref resolve to the wrong value."""
    monkeypatch.setattr("chronotrace.store.valuepool.digest", lambda _c: b"\x00" * 16)
    pool = ValuePoolWriter()
    pool.add(capture({"real": "value"}))
    with pytest.raises(PoolCollision):
        pool.add(capture({"different": "value"}))  # same forged hash, different bytes


def test_forced_equal_hash_with_equal_content_is_not_a_collision(monkeypatch: Any) -> None:
    monkeypatch.setattr("chronotrace.store.valuepool.digest", lambda _c: b"\x00" * 16)
    pool = ValuePoolWriter()
    r1 = pool.add(capture({"x": 1}))
    r2 = pool.add(capture({"x": 1}))  # same hash AND same bytes: a legitimate dedup hit
    assert r1 == r2
    assert len(pool) == 1


def test_empty_pool_round_trips() -> None:
    pool = ValuePoolWriter()
    assert len(pool) == 0
    assert decode_pool(pool.encode()) == []


def test_complex_values_round_trip() -> None:
    """capture() emits `complex` as a bare atom; msgpack has no native complex, so the
    pool tags and reconstructs it -- otherwise packing would crash on a complex local."""
    pool = ValuePoolWriter()
    ref = pool.add(capture(3 + 4j))
    assert _round_trip(pool)[ref] == 3 + 4j


def test_nested_captured_structure_round_trips() -> None:
    pool = ValuePoolWriter()
    original = capture({"users": [{"id": 1, "tags": ["a", "b"]}, {"id": 2, "tags": []}]})
    ref = pool.add(original)
    assert _round_trip(pool)[ref] == original


# ---------------------------------------------------------------------------
# decode_pool parses untrusted input
# ---------------------------------------------------------------------------


def test_a_count_over_the_cap_is_rejected() -> None:
    forged = struct.pack("<I", MAX_VALUES + 1)
    with pytest.raises(ValueError, match="over the"):
        decode_pool(forged)


def test_a_directory_entry_pointing_past_the_block_is_rejected() -> None:
    # count=1, one directory entry claiming offset 0 length 10^9, but no value bytes.
    forged = struct.pack("<I", 1) + struct.pack("<Q I", 0, 1_000_000_000)
    with pytest.raises(ValueError, match="overruns"):
        decode_pool(forged)


def test_a_directory_that_overruns_the_block_is_rejected() -> None:
    forged = struct.pack("<I", 100)  # claims 100 entries, carries no directory
    with pytest.raises(ValueError, match="directory overruns"):
        decode_pool(forged)
