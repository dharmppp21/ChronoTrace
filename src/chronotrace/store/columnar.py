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

COUNT_SIZE = _COUNT.size  # 4; the u32 event-count prefix. Kept uncompressed by the
# writer so the reader can index by seq (peek_count) without decompressing a block.

_RAW, _RLE, _DELTA_RLE = 0, 1, 2

MAX_EVENTS_PER_BLOCK = 1 << 20
"""Hard cap on the events one block may claim, so decoding *untrusted* input cannot
be tricked into an unbounded allocation. The writer's default is 65536, so 1M is
16x headroom; a block claiming more is corrupt or hostile and is rejected. `decode`
is a trust boundary -- a hostile file can compute a valid CRC over malformed bytes,
so the CRC does not make the payload safe, and every length the payload declares
must be bounded here."""


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


def _rle_decode(data: bytes, limit: int) -> list[int]:
    """Expand `(value, run)` pairs, refusing to produce more than `limit` values.

    The bound is the whole security of this function: `run` is a number read from
    an untrusted file, and `[value] * run` with a hostile `run` is an OOM. A run
    that would push the output past `limit` (the block's declared event count) is a
    corrupt payload, not data.
    """
    pairs = iter(_unpack_i64(data))
    out: list[int] = []
    for value, run in zip(pairs, pairs, strict=False):  # pair up; a trailing odd is dropped
        if run < 0 or len(out) + run > limit:
            raise ValueError("RLE run exceeds the block's declared event count")
        out.extend([value] * run)
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


def _decode_column(codec: int, data: bytes, limit: int) -> list[int]:
    """Decode one column, bounded to at most `limit` values.

    Raw is inherently bounded (`len(data) // 8` values, and `data` is a slice of a
    file-bounded payload); the RLE codecs need the explicit `limit`.
    """
    if codec == _RAW:
        return _unpack_i64(data)
    if codec == _RLE:
        return _rle_decode(data, limit)
    if codec == _DELTA_RLE:
        return list(accumulate(_rle_decode(data, limit)))
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


def peek_count(buf: object, payload_offset: int) -> int:
    """The event count of an EVENTS block, without decoding it.

    The reader calls this to size its seq index lazily -- reading 4 bytes per block
    instead of touching every page. The value is untrusted (no CRC yet); the caller
    bounds it against `MAX_EVENTS_PER_BLOCK`.

    Args:
        buf: any buffer (bytes or mmap) holding the block.
        payload_offset: file offset of the payload's first byte (block offset +
            frame size). The caller must have checked it is in bounds.
    """
    return int(_COUNT.unpack_from(buf, payload_offset)[0])  # type: ignore[arg-type]


def decode_events(payload: bytes) -> list[Event]:
    """Inverse of `encode_events`. Reconstructs the exact events, `None`s restored.

    **Parses untrusted input.** A hostile file can carry a valid CRC over malformed
    bytes, so this bounds every allocation the payload requests: the declared event
    count is capped at `MAX_EVENTS_PER_BLOCK`, and each column may expand to at most
    that count. A short or truncated slice yields a `struct.error`; the reader turns
    both that and the `ValueError`s below into `CorruptRecording`.

    Raises:
        ValueError: an out-of-range count, an over-long RLE run, a column of the
            wrong length, or an unknown codec.
        struct.error: the payload is too short to hold a header it declares.

    Complexity: O(fields x events).
    """
    (count,) = _COUNT.unpack_from(payload, 0)
    if not 0 <= count <= MAX_EVENTS_PER_BLOCK:
        raise ValueError(f"block claims {count} events, over the {MAX_EVENTS_PER_BLOCK} cap")
    pos = _COUNT.size
    columns: dict[str, list[int]] = {}
    for field in _FIELDS:
        codec, length = _COL_HEADER.unpack_from(payload, pos)
        pos += _COL_HEADER.size
        values = _decode_column(codec, payload[pos : pos + length], count)
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
