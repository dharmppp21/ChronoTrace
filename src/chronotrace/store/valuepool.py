"""The content-addressed value pool: where a captured value lives on disk, once.

Pool is truth; the cache is an accelerator
------------------------------------------
The recorder's dedup cache (`recorder/dedup.py`) makes *recording* fast by
remembering where recently-seen content was stored, and it evicts under memory
pressure. This pool is the durable record: every distinct value stored exactly once,
addressed by the same Day-8 content hash, never evicted. An evicted cache entry
therefore costs the recorder a re-hash, never a wrong value -- because a `ValueRef` is
resolved against the pool, which is complete, not against the cache, which is partial.
That split is the whole relationship: the cache is an optimisation you can throw away,
the pool is the recording.

Collision policy: fail loud, never resolve wrong
------------------------------------------------
Two different values sharing one 128-bit hash would make a `ValueRef` resolve to the
*wrong* value -- silent, indistinguishable from a real recording, the one unforgivable
failure for a debugger (dedup.py sizes the probability at ~1e-25 for 10^7 values). So
"negligible" is enforced, not assumed: `add` verifies on every repeat that a hash it
has already seen carries byte-identical content, and raises `PoolCollision` if not.
That turns a hash collision -- or, far more likely, a canonicalisation bug upstream --
into a loud failure at write time instead of a wrong value shown at read time.

On-disk shape (spec §6.4)
-------------------------
`[u32 value_count][directory: value_count x (u64 offset, u32 length)][value bytes]`.
Each value is msgpack, restricted to the capturer's closed type set (pickle is banned
at the format level, spec §9). A `ValueRef` in an event is an index into the directory.
"""

from __future__ import annotations

import struct
from typing import Any, Final

import msgpack

from chronotrace.recorder.capture import CapturedValue
from chronotrace.recorder.dedup import digest
from chronotrace.recorder.values import ValueRef

_COUNT = struct.Struct("<I")
_DIR_ENTRY = struct.Struct("<Q I")  # offset (u64), length (u32), within the value-bytes region
_COMPLEX_TAG = "complex"  # capture emits complex as a bare atom; msgpack has no native complex

MAX_VALUES: Final = 1 << 24
"""Untrusted-input bound: a VALUES block claiming more than 16M values is rejected
before its directory is walked. A real pool of distinct values is far smaller; this
only stops a hostile `value_count` from sizing an allocation."""


class PoolCollision(ValueError):
    """Two distinct values hashed to the same content address.

    Refuses to store the second rather than let a `ValueRef` later resolve to the
    wrong value. See the module docstring: loud at write beats wrong at read.
    """


class ValuePoolWriter:
    """Accumulates captured values write-once, then encodes the VALUES section.

    `add` is content-addressed: identical content returns the existing reference
    (stored once, referenced many times -- the collapse that makes recording
    affordable), new content is appended at the next dense index. Because the dedup is
    on content, a value that recurs across two flushes still lands once and both refs
    resolve, without the caller tracking what it has already written.
    """

    __slots__ = ("_blobs", "_index")

    def __init__(self) -> None:
        self._blobs: list[bytes] = []
        self._index: dict[bytes, ValueRef] = {}

    def add(self, captured: CapturedValue) -> ValueRef:
        """Store `captured` once; return its reference. Verifies content on a hash hit.

        Raises:
            PoolCollision: a previously stored value shares this content hash but has
                different bytes -- a collision or an upstream canonicalisation bug.

        Complexity: O(size of the captured representation) for the hash and msgpack.
        """
        key = digest(captured)
        blob = _pack(captured)
        existing = self._index.get(key)
        if existing is not None:
            if self._blobs[existing] != blob:
                raise PoolCollision(
                    f"two distinct values share content hash {key.hex()}; refusing to corrupt pool"
                )
            return existing
        ref = ValueRef(len(self._blobs))
        self._blobs.append(blob)
        self._index[key] = ref
        return ref

    def encode(self) -> bytes:
        """The VALUES section payload (spec §6.4): count, directory, then value bytes."""
        directory = bytearray()
        body = bytearray()
        for blob in self._blobs:
            directory += _DIR_ENTRY.pack(len(body), len(blob))
            body += blob
        return _COUNT.pack(len(self._blobs)) + bytes(directory) + bytes(body)

    def __len__(self) -> int:
        return len(self._blobs)


def decode_pool(payload: bytes) -> list[bytes]:
    """Decode a VALUES section into raw msgpack blobs, indexable by `ValueRef`.

    **Parses untrusted input.** The declared `value_count` is capped at `MAX_VALUES`,
    the directory is checked to fit the block, and every `(offset, length)` is bounds-
    checked against the payload before slicing -- a hostile directory entry pointing
    past the block raises `ValueError`, never reads out of range. Resolve a blob to a
    value with `unpack_value`.

    Raises:
        ValueError: the count is over the cap, or a directory entry is out of bounds.
        struct.error: the payload is too short for the header or directory it declares.
    """
    (count,) = _COUNT.unpack_from(payload, 0)
    if not 0 <= count <= MAX_VALUES:
        raise ValueError(f"value pool claims {count} values, over the {MAX_VALUES} cap")
    dir_end = _COUNT.size + count * _DIR_ENTRY.size
    if dir_end > len(payload):
        raise ValueError("value pool directory overruns the block")
    blobs: list[bytes] = []
    for i in range(count):
        offset, length = _DIR_ENTRY.unpack_from(payload, _COUNT.size + i * _DIR_ENTRY.size)
        start = dir_end + offset
        end = start + length
        if end > len(payload):
            raise ValueError(f"value {i} at [{offset}, {offset + length}) overruns the block")
        blobs.append(payload[start:end])
    return blobs


def unpack_value(blob: bytes) -> CapturedValue:
    """Decode one msgpack value blob back to a captured representation.

    `strict_map_key=False` because captured maps may key on non-strings; the closed
    type set means no code is constructed (spec §9).
    """
    return msgpack.unpackb(blob, object_hook=_object_hook, strict_map_key=False)


def _pack(captured: CapturedValue) -> bytes:
    return msgpack.packb(captured, default=_default)  # type: ignore[no-any-return]


def _default(obj: object) -> dict[str, Any]:
    if isinstance(obj, complex):
        return {"$": _COMPLEX_TAG, "real": obj.real, "imag": obj.imag}
    raise TypeError(f"value pool cannot serialise {type(obj).__name__}")


def _object_hook(obj: dict[Any, Any]) -> Any:
    if obj.get("$") == _COMPLEX_TAG:
        return complex(obj["real"], obj["imag"])
    return obj
