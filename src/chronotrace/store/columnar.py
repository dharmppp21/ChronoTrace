"""Columnar encoding of an event batch -- the EVENTS block payload.

N events go in row by row; they come out column by column (all `seq` together,
then all `kind`, ...), each column run through the cheapest of three integer
codecs. This is the core of [ADR-0004](../../../docs/adr/0004-chrono-file-format.md):
laying like fields next to like turns the event stream into something a general
compressor (zstd, day 14) crushes -- `seq` deltas to a run of ones, `kind` to a
handful of runs, `code_id` stays constant while execution sits in one function.
Measured 7-12x smaller than row before zstd even runs.

Payload layout (spec §6.3): `u32 event_count`, then ten columns in the field order
of `Event`, each `[u8 codec][u32 byte_length][bytes]`. A `None` field is stored as
`-1` (`name_id`/`value_ref`/`exc_type_id` are otherwise non-negative indices).

Codec choice is per column and automatic: encode tries all three and keeps the
smallest, so no hand-tuned per-field policy can be wrong for unexpected data -- and
a stream with *dropped* events (whose `seq` is no longer a clean +1) still encodes,
because the delta codec stores whatever the gaps are and raw is always a fallback.

The three codecs
----------------
* **raw** -- the values as int64. The safe fallback; wins on incompressible columns.
* **rle** -- run-length `(value, count)` pairs. Crushes constant columns
  (`thread_id`, `kind`, and the `-1` runs of `name_id`/`value_ref`).
* **delta-rle** -- run-length of the *consecutive differences*. Crushes monotonic
  and constant-stride columns (`seq` is `+1` -> deltas are one long run of `1`;
  `timestamp_ns` climbs by a near-constant step). Plain delta without the RLE would
  not shrink the bytes pre-zstd -- a run of `1`s is still a run of int64s -- so the
  two are composed. zstd (day 14) then compresses whatever survives further.
"""

from __future__ import annotations

import struct
import sys
from array import array
from itertools import accumulate

from chronotrace.recorder.events import Event, EventKind
from chronotrace.recorder.values import ValueRef

# Field order is the on-disk column order and must match the spec. `None` -> -1.
_FIELDS = (
    "seq",
    "kind",
    "timestamp_ns",
    "thread_id",
    "frame_id",
    "code_id",
    "lineno",
    "name_id",
    "value_ref",
    "exc_type_id",
)

_HOST_LE = sys.byteorder == "little"
_COUNT = struct.Struct("<I")
_COL_HEADER = struct.Struct("<B I")  # codec, byte_length

_RAW, _RLE, _DELTA_RLE = 0, 1, 2


def _pack_i64(values: list[int]) -> bytes:
    """`values` as little-endian int64 -- explicitly, never host order.

    `array('q').tobytes()` is *host* byte order, so it would write big-endian on a
    big-endian machine and break the spec's "always little-endian" rule. array is
    used for speed (C-level) and byte-swapped on the rare big-endian host.
    """
    buf = array("q", values)
    if not _HOST_LE:
        buf.byteswap()
    return buf.tobytes()


def _unpack_i64(data: bytes) -> list[int]:
    buf = array("q")
    buf.frombytes(data)
    if not _HOST_LE:
        buf.byteswap()
    return buf.tolist()


def _rle_encode(values: list[int]) -> bytes:
    """(value, run-length) pairs as int64. Small when a column holds long runs."""
    pairs: list[int] = []
    i, n = 0, len(values)
    while i < n:
        j = i + 1
        while j < n and values[j] == values[i]:
            j += 1
        pairs.append(values[i])
        pairs.append(j - i)
        i = j
    return _pack_i64(pairs)


def _rle_decode(data: bytes) -> list[int]:
    pairs = _unpack_i64(data)
    out: list[int] = []
    for k in range(0, len(pairs), 2):
        out.extend([pairs[k]] * pairs[k + 1])
    return out


def _deltas(values: list[int]) -> list[int]:
    out, prev = [], 0
    for v in values:
        out.append(v - prev)
        prev = v
    return out


def _encode_column(values: list[int]) -> tuple[int, bytes]:
    """The smallest of raw / rle / delta-rle for one column. Raw is the fallback."""
    candidates = [(_RAW, _pack_i64(values))]
    if values:  # rle of an empty column is empty; skip the needless candidates
        candidates.append((_RLE, _rle_encode(values)))
        candidates.append((_DELTA_RLE, _rle_encode(_deltas(values))))
    return min(candidates, key=lambda c: len(c[1]))


def _decode_column(codec: int, data: bytes) -> list[int]:
    if codec == _RAW:
        return _unpack_i64(data)
    if codec == _RLE:
        return _rle_decode(data)
    if codec == _DELTA_RLE:
        return list(accumulate(_rle_decode(data)))
    raise ValueError(f"unknown column codec {codec}")


def encode_events(events: list[Event]) -> bytes:
    """Encode a batch of events as an EVENTS block payload.

    Complexity: O(fields x events) -- three linear codec passes per column.
    """
    out = bytearray(_COUNT.pack(len(events)))
    for field in _FIELDS:
        column = [_field_int(e, field) for e in events]
        codec, data = _encode_column(column)
        out += _COL_HEADER.pack(codec, len(data))
        out += data
    return bytes(out)


def decode_events(payload: bytes) -> list[Event]:
    """Inverse of `encode_events`. Reconstructs the exact events, `None`s restored.

    Raises:
        ValueError: a column decodes to the wrong length or an unknown codec -- a
            malformed EVENTS payload (the framing CRC makes this rare).

    Complexity: O(fields x events).
    """
    (count,) = _COUNT.unpack_from(payload, 0)
    pos = _COUNT.size
    columns: dict[str, list[int]] = {}
    for field in _FIELDS:
        codec, length = _COL_HEADER.unpack_from(payload, pos)
        pos += _COL_HEADER.size
        values = _decode_column(codec, payload[pos : pos + length])
        if len(values) != count:
            raise ValueError(f"column {field!r} has {len(values)} values, expected {count}")
        columns[field] = values
        pos += length
    return [_row(columns, i) for i in range(count)]


def _field_int(event: Event, field: str) -> int:
    value = getattr(event, field)
    if value is None:
        return -1
    return int(value)


def _row(columns: dict[str, list[int]], i: int) -> Event:
    def opt(field: str) -> int | None:
        v = columns[field][i]
        return None if v == -1 else v

    return Event(
        seq=columns["seq"][i],
        kind=EventKind(columns["kind"][i]),
        timestamp_ns=columns["timestamp_ns"][i],
        thread_id=columns["thread_id"][i],
        frame_id=columns["frame_id"][i],
        code_id=columns["code_id"][i],
        lineno=columns["lineno"][i],
        name_id=opt("name_id"),
        value_ref=None if columns["value_ref"][i] == -1 else ValueRef(columns["value_ref"][i]),
        exc_type_id=opt("exc_type_id"),
    )
