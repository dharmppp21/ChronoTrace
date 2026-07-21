"""Read a `.chrono` file back: memory-mapped, lazily decoded, and defensive.

**This file parses untrusted input.** A recording arrives in a stranger's bug
report, so every length and offset in it is treated as hostile until checked
against the file's real size, and every allocation the file requests is bounded.
A malformed or malicious `.chrono` must produce a precise `ChronoError` -- never a
crash, a hang, or an out-of-memory. That is the whole job of this file, alongside
being fast.

Why mmap (and not `read()` or `seek`+`read`)
--------------------------------------------
`read()`-everything loads a 10 GB recording into 10 GB of RSS to answer one query.
`seek`+`read` per event is a syscall per access, which the timeline scrubber --
thousands of `__getitem__`s as the playhead drags -- cannot afford. `mmap` makes
the OS page cache *be* our cache: opening a 10 GB file costs almost no RSS, and we
fault in only the pages we actually touch. The reader is opened **read-only**
(`ACCESS_READ`), which is a safety property, not a limitation -- it structurally
cannot corrupt a recording, and a hostile file cannot trick it into writing.

Costs, stated honestly: the mmap is a snapshot of the file's size at open, so a
file being concurrently appended is read up to that size (a still-writing recording
opens as its readable prefix -- exactly the crash case). And Windows mmap differs
from POSIX: a zero-length file cannot be mapped, so an empty file is caught before
mmap and raised as `TruncatedRecording`.

Lazy blocks with an LRU of *decoded* blocks
-------------------------------------------
A block is decoded only when an event inside it is asked for, and the last
`cache_blocks` decoded blocks are kept. Decoding a whole ~65k-event block to return
one event is *right* here: sequential scrubbing is the dominant access pattern, so
the next request almost always lands in the same block, and the LRU makes that a
dictionary hit. Combined with `MAX_EVENTS_PER_BLOCK`, the retained memory is bounded
at `cache_blocks x MAX_EVENTS_PER_BLOCK` events even for a hostile file.

The cache holds the fully-*decoded* events, i.e. post-decompression and
post-columnar-decode -- deliberately, not the compressed bytes. Caching decompressed
blocks is what matters for scrubbing: a repeat hit on a block (the common case as the
playhead drags) skips *both* the zstd decompress and the columnar decode, not just a
re-read. Caching compressed bytes would still pay the decode on every access and save
only an mmap fault the OS page cache already handles.

Transparent decompression
--------------------------
Day 14's compression is invisible above this layer. `decode_block` returns whatever
bytes are on disk (verified against the block CRC); if the block flags say
`COMPRESSED_ZSTD`, `_raw_payload` runs the bounded decompressor before the payload is
decoded. A decompression bomb or a corrupt frame becomes a `CorruptRecording`, same as
any other bad block.
"""

from __future__ import annotations

import mmap
import os
import struct
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Iterator
from types import TracebackType
from typing import BinaryIO

from chronotrace.recorder.capture import CapturedValue
from chronotrace.recorder.events import Event
from chronotrace.store.columnar import COUNT_SIZE, MAX_EVENTS_PER_BLOCK, decode_events, peek_count
from chronotrace.store.compression import decompress
from chronotrace.store.constants import (
    BLOCK_HEADER_SIZE,
    EOCD,
    EOCD_MAGIC,
    EOCD_SIZE,
    FORMAT_VERSION_MAJOR,
    HEADER,
    HEADER_SIZE,
    INDEX_ENTRY,
    MAGIC,
    BlockFlag,
    BlockType,
    EocdFlag,
)
from chronotrace.store.delta import DELTA_RANGE, DELTA_RANGE_SIZE, Delta, decode_deltas
from chronotrace.store.errors import CorruptRecording, TruncatedRecording, UnsupportedVersion
from chronotrace.store.framing import BlockError, decode_block
from chronotrace.store.keyframe import KF_SEQ, KF_SEQ_SIZE, Keyframe, decode_keyframe
from chronotrace.store.recovery import walk_blocks
from chronotrace.store.valuepool import decode_pool, unpack_value

DEFAULT_CACHE_BLOCKS = 8

# (start_seq, block_offset, event_count) for one EVENTS block. The block length is
# not kept -- decode_block re-reads it from the frame, so storing it would be dead.
_Block = tuple[int, int, int]


class ChronoReader:
    """A read-only, lazily-decoded view over a `.chrono` file. Parses untrusted input.

    Open with `ChronoReader.open(path)` (mmap) or `ChronoReader.from_bytes(data)`
    (an in-memory buffer, for small files and tests). Index by `seq`
    (`reader[seq]`), slice (`reader[a:b]`), iterate (`iter_events()`), or take the
    length (`len(reader)`). Use it as a context manager so the mmap is released.
    """

    __slots__ = (
        "_blocks",
        "_buf",
        "_cache",
        "_cache_blocks",
        "_count",
        "_delta_blocks",
        "_delta_cache",
        "_file",
        "_keyframes",
        "_mmap",
        "_pool",
        "_starts",
        "_truncated",
        "_values_offset",
    )

    def __init__(
        self,
        buf: bytes | mmap.mmap,
        *,
        mmap_obj: mmap.mmap | None = None,
        file: BinaryIO | None = None,
        cache_blocks: int = DEFAULT_CACHE_BLOCKS,
    ) -> None:
        self._buf = buf
        self._mmap = mmap_obj
        self._file = file
        self._cache_blocks = cache_blocks
        self._cache: OrderedDict[int, list[Event]] = OrderedDict()
        self._delta_cache: OrderedDict[int, list[Delta]] = OrderedDict()
        self._blocks: list[_Block] = []
        self._starts: list[int] = []
        self._count = 0
        self._truncated = False
        self._values_offset: int | None = None
        self._pool: list[bytes] | None = None  # the decoded value pool, decoded once, lazily
        self._keyframes: list[tuple[int, int]] = []  # (seq, block_offset), sorted by seq
        self._delta_blocks: list[tuple[int, int, int]] = []  # (first_seq, last_seq, offset)
        self._validate_header()
        self._build_index()

    # -- construction -------------------------------------------------------

    @classmethod
    def open(
        cls, path: str | os.PathLike[str], *, cache_blocks: int = DEFAULT_CACHE_BLOCKS
    ) -> ChronoReader:
        """Memory-map `path` read-only and open it. The caller must `close()`."""
        file = open(path, "rb")  # noqa: SIM115, PTH123 -- lifetime is the reader's
        try:
            if os.fstat(file.fileno()).st_size == 0:
                raise TruncatedRecording(f"{path}: file is empty")
            buf = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
        except BaseException:
            file.close()
            raise
        try:
            return cls(buf, mmap_obj=buf, file=file, cache_blocks=cache_blocks)
        except BaseException:
            buf.close()
            file.close()
            raise

    @classmethod
    def from_bytes(cls, data: bytes, *, cache_blocks: int = DEFAULT_CACHE_BLOCKS) -> ChronoReader:
        """Open an in-memory `.chrono` buffer -- no file, no mmap."""
        return cls(data, cache_blocks=cache_blocks)

    def _validate_header(self) -> None:
        if len(self._buf) < HEADER_SIZE:
            raise TruncatedRecording(
                f"file is {len(self._buf)} bytes, smaller than a {HEADER_SIZE}-byte header"
            )
        magic, major, minor, _flags, _hsize = HEADER.unpack_from(self._buf, 0)
        if magic != MAGIC:
            raise CorruptRecording("not a .chrono file: bad magic")
        if major > FORMAT_VERSION_MAJOR:
            raise UnsupportedVersion(
                f"file is format v{major}.{minor}; this reader supports up to "
                f"v{FORMAT_VERSION_MAJOR}.x -- upgrade ChronoTrace"
            )

    def _build_index(self) -> None:
        footer = self._blocks_from_footer()
        if footer is not None:
            locations, self._truncated = footer
        else:
            locations, self._truncated = self._blocks_from_scan(), True
        seq = 0
        for offset, count in locations:
            self._blocks.append((seq, offset, count))
            seq += count
        self._count = seq
        self._starts = [start for start, *_ in self._blocks]
        self._keyframes.sort()  # emitted in seq order, but sort defends a rewritten index
        self._delta_blocks.sort()

    # -- index: footer (lazy) or scan (crash recovery) ----------------------
    #
    # Both paths record the VALUES offset and the keyframe (seq, offset) list as side
    # effects on the instance, and return only the EVENTS locations the seq index
    # needs -- keeping those two "extra" outputs off the return tuple.

    def _blocks_from_footer(self) -> tuple[list[tuple[int, int]], bool] | None:
        """EVENTS blocks (offset, count) from the footer, or None to fall back to scan.

        None when the footer is absent, out of bounds, corrupt, or claims an
        impossible EVENTS count. An invalid *optional* (VALUES/KEYFRAMES) entry is
        skipped rather than distrusting the whole footer.
        """
        buf, size = self._buf, len(self._buf)
        if size < EOCD_SIZE:
            return None
        index_offset, _len, _crc, flags, magic = EOCD.unpack_from(buf, size - EOCD_SIZE)
        if magic != EOCD_MAGIC or not self._in_bounds(index_offset, BLOCK_HEADER_SIZE):
            return None
        try:
            block_type, _flags, index_payload, _next = decode_block(buf, index_offset)
        except BlockError:
            return None
        if block_type != BlockType.INDEX:
            return None
        locations: list[tuple[int, int]] = []
        for k in range(0, len(index_payload) - INDEX_ENTRY.size + 1, INDEX_ENTRY.size):
            entry_type, offset, length = INDEX_ENTRY.unpack_from(index_payload, k)
            if entry_type == BlockType.VALUES:
                if self._in_bounds(offset, length):
                    self._values_offset = offset
            elif entry_type == BlockType.KEYFRAMES:
                self._record_keyframe(offset, length)
            elif entry_type == BlockType.DELTAS:
                self._record_delta_block(offset, length)
            elif entry_type == BlockType.EVENTS:
                count = self._safe_count(offset, length)
                if count is None:
                    return None  # a bad required entry: distrust the footer, scan instead
                locations.append((offset, count))
        return locations, bool(flags & EocdFlag.TRUNCATED)

    def _blocks_from_scan(self) -> list[tuple[int, int]]:
        """Recover EVENTS blocks by walking intact frames from the header (crash path).

        Delegates the CRC-checked frame walk and torn-tail classification to
        `recovery.walk_blocks` -- the one place that logic lives -- then indexes the
        surviving blocks by type. VALUES, keyframes and deltas written before the crash
        are recovered too, so seeking still works on a crashed recording.
        """
        blocks, _status = walk_blocks(self._buf)
        locations: list[tuple[int, int]] = []
        for offset, block_type, _flags, nxt in blocks:
            if block_type == BlockType.EVENTS:
                count = self._safe_count(offset, nxt - offset)
                if count is None:
                    break  # a CRC-valid but impossible count: stop at the good prefix
                locations.append((offset, count))
            elif block_type == BlockType.VALUES:
                self._values_offset = offset
            elif block_type == BlockType.KEYFRAMES:
                self._record_keyframe(offset, nxt - offset)
            elif block_type == BlockType.DELTAS:
                self._record_delta_block(offset, nxt - offset)
        return locations

    def _record_keyframe(self, offset: int, length: int) -> None:
        """Index a KEYFRAMES block by its peeked `seq`, skipping it if out of bounds."""
        if not self._in_bounds(offset, length):
            return
        start = offset + BLOCK_HEADER_SIZE
        if start + KF_SEQ_SIZE > len(self._buf):
            return
        kf_seq = int(KF_SEQ.unpack_from(self._buf, start)[0])
        self._keyframes.append((kf_seq, offset))

    def _record_delta_block(self, offset: int, length: int) -> None:
        """Index a DELTAS block by its peeked (first_seq, last_seq), skipping if OOB."""
        if not self._in_bounds(offset, length):
            return
        start = offset + BLOCK_HEADER_SIZE
        if start + DELTA_RANGE_SIZE > len(self._buf):
            return
        first, last = DELTA_RANGE.unpack_from(self._buf, start)
        self._delta_blocks.append((int(first), int(last), offset))

    def _in_bounds(self, offset: int, length: int) -> bool:
        return (
            offset >= HEADER_SIZE
            and length >= BLOCK_HEADER_SIZE
            and offset + length <= len(self._buf)
        )

    def _safe_count(self, offset: int, length: int) -> int | None:
        """A block's event count, or None if the block or count is out of bounds."""
        if not self._in_bounds(offset, length):
            return None
        if offset + BLOCK_HEADER_SIZE + COUNT_SIZE > len(self._buf):
            return None
        count = peek_count(self._buf, offset + BLOCK_HEADER_SIZE)
        return count if 0 <= count <= MAX_EVENTS_PER_BLOCK else None

    # -- access -------------------------------------------------------------

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, key: int | slice) -> Event | list[Event]:
        """The event at `seq`, or a list for a slice.

        Complexity: O(log B) to locate the block by `seq` (binary search over block
        start-seqs) plus O(1) to index within it -- the operation the timeline
        scrubber calls thousands of times as the playhead drags.
        """
        if isinstance(key, slice):
            return [self._at(i) for i in range(*key.indices(self._count))]
        return self._at(key)

    def _at(self, seq: int) -> Event:
        if seq < 0:
            seq += self._count
        if not 0 <= seq < self._count:
            raise IndexError(f"seq {seq} out of range [0, {self._count})")
        start, offset, _count = self._blocks[bisect_right(self._starts, seq) - 1]
        events = self._decode(offset)
        local = seq - start
        if local >= len(events):
            raise IndexError(f"seq {seq} beyond its block's real contents")  # a lying index
        return events[local]

    def iter_events(self) -> Iterator[Event]:
        """Stream every event forward with bounded memory -- safe over a 10 GB file.

        One block is decoded at a time and older blocks are evicted by the LRU.
        """
        for _start, offset, _count in self._blocks:
            yield from self._decode(offset)

    def _decode(self, offset: int) -> list[Event]:
        cached = self._cache.get(offset)
        if cached is not None:
            self._cache.move_to_end(offset)
            return cached
        try:
            _bt, flags, payload, _next = decode_block(self._buf, offset)  # CRC-checked
            events = decode_events(self._events_payload(flags, payload))  # bounded (columnar.py)
        except (BlockError, ValueError, struct.error) as exc:
            raise CorruptRecording(f"block at offset {offset}: {exc}") from exc
        self._cache[offset] = events
        if len(self._cache) > self._cache_blocks:
            self._cache.popitem(last=False)
        return events

    def _events_payload(self, flags: int, payload: bytes) -> bytes:
        """A columnar EVENTS payload, decompressing the columns behind the raw count.

        The u32 count is stored uncompressed (so `peek_count` can index by seq without
        decompressing); only the columns after it are the compression frame. VALUES has
        no such prefix, so it decompresses whole. `decompress` is bounded, so a bomb or
        corrupt frame raises a `ValueError` subclass the callers turn into
        `CorruptRecording`.
        """
        if flags & BlockFlag.COMPRESSED_ZSTD:
            return payload[:COUNT_SIZE] + decompress(payload[COUNT_SIZE:])
        return payload

    # -- values -------------------------------------------------------------

    def value(self, ref: int) -> CapturedValue:
        """Resolve a `value_ref` (from a VAR_WRITE event) to its captured value.

        The VALUES section is decoded once and its blob list cached; this decodes the
        single msgpack blob asked for. The pool is the durable truth a ref resolves
        against -- see `valuepool.py`.

        Raises:
            IndexError: `ref` is outside the pool.
            CorruptRecording: the VALUES block is damaged or hostile.
        """
        blobs = self._pool_blobs()
        if not 0 <= ref < len(blobs):
            raise IndexError(f"value_ref {ref} out of range [0, {len(blobs)})")
        return unpack_value(blobs[ref])

    def _pool_blobs(self) -> list[bytes]:
        if self._pool is not None:
            return self._pool
        if self._values_offset is None:
            self._pool = []  # a recording with no captured values (control-flow only)
            return self._pool
        try:
            _bt, flags, payload, _next = decode_block(self._buf, self._values_offset)  # CRC-checked
            section = decompress(payload) if flags & BlockFlag.COMPRESSED_ZSTD else payload
            self._pool = decode_pool(section)  # allocations bounded (valuepool.py)
        except (BlockError, ValueError, struct.error) as exc:
            raise CorruptRecording(f"value pool at offset {self._values_offset}: {exc}") from exc
        return self._pool

    # -- keyframes ----------------------------------------------------------

    def keyframe_count(self) -> int:
        """How many keyframes the recording carries. Zero for a 1.1-or-older file."""
        return len(self._keyframes)

    def nearest_keyframe_at_or_before(self, seq: int) -> Keyframe | None:
        """The nearest keyframe whose `seq` is <= `seq`, decoded, or None if none is.

        The operation the scrubber calls on every drag: O(log K) binary search over
        keyframe seqs to find the floor, then one block decode. If that keyframe's
        block is corrupt, it falls back to the previous keyframe and returns that --
        the caller then replays a little further. Graceful degradation: a torn
        keyframe should cost latency, never the answer.

        Complexity: O(log K) plus one keyframe decode (retried on corruption).
        """
        i = bisect_right(self._keyframes, seq, key=lambda kf: kf[0]) - 1
        while i >= 0:
            kf_seq, offset = self._keyframes[i]
            try:
                return self._decode_keyframe(offset, kf_seq)
            except CorruptRecording:
                i -= 1  # this keyframe is torn; step back to the previous good one
        return None

    def _decode_keyframe(self, offset: int, seq: int) -> Keyframe:
        try:
            _bt, flags, payload, _next = decode_block(self._buf, offset)  # CRC-checked
            body = payload[KF_SEQ_SIZE:]  # drop the uncompressed seq prefix
            raw = decompress(body) if flags & BlockFlag.COMPRESSED_ZSTD else body
            return decode_keyframe(raw, seq)  # allocations bounded (keyframe.py)
        except (BlockError, ValueError, struct.error) as exc:
            raise CorruptRecording(f"keyframe at offset {offset}: {exc}") from exc

    # -- deltas -------------------------------------------------------------

    def deltas_between(self, seq_a: int, seq_b: int) -> list[Delta]:
        """The invertible deltas with `seq_a <= seq <= seq_b`, in `seq` order.

        This is the second half of the codec: from a keyframe at or before `seq_a`,
        applying these reaches any instant in the span, and inverting them steps back.
        Only the DELTAS blocks whose stored span overlaps `[seq_a, seq_b]` are decoded,
        so a bounded query (a keyframe interval) touches a bounded number of blocks --
        which is what keeps reconstruction cost bounded.

        Complexity: O(overlapping blocks x block size). For a within-interval span that
        is one or two blocks.
        """
        out: list[Delta] = []
        for first, last, offset in self._delta_blocks:
            if first > seq_b:
                break  # sorted by first_seq: no later block can overlap
            if last < seq_a:
                continue
            out.extend(d for d in self._decode_delta_block(offset) if seq_a <= d.seq <= seq_b)
        return out

    def _decode_delta_block(self, offset: int) -> list[Delta]:
        """Decode one DELTAS block, memoised by the same LRU discipline as EVENTS blocks.

        Reconstruction and a playhead drag hit the *same* block over and over (a step is
        one event; a block spans thousands), so decoding it per call dominated the
        measured step latency. Caching the decoded deltas -- the most-processed form --
        turns a repeat into a dict hit, exactly as `_decode` does for events.
        """
        cached = self._delta_cache.get(offset)
        if cached is not None:
            self._delta_cache.move_to_end(offset)
            return cached
        try:
            _bt, flags, payload, _next = decode_block(self._buf, offset)  # CRC-checked
            body = payload[DELTA_RANGE_SIZE:]  # drop the uncompressed (first, last) span
            raw = decompress(body) if flags & BlockFlag.COMPRESSED_ZSTD else body
            deltas = decode_deltas(raw)  # allocations bounded (delta.py)
        except (BlockError, ValueError, struct.error) as exc:
            raise CorruptRecording(f"delta block at offset {offset}: {exc}") from exc
        self._delta_cache[offset] = deltas
        if len(self._delta_cache) > self._cache_blocks:
            self._delta_cache.popitem(last=False)
        return deltas

    # -- lifecycle ----------------------------------------------------------

    @property
    def truncated(self) -> bool:
        """Whether the recording is incomplete: dropped events, or a scanned crash.

        The tail beyond the readable prefix is absent.
        """
        return self._truncated

    def close(self) -> None:
        """Release the mmap and file. Idempotent."""
        self._cache.clear()
        self._delta_cache.clear()
        self._pool = None
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> ChronoReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
