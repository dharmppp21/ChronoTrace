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

Lazy blocks with an LRU
-----------------------
A block is decoded only when an event inside it is asked for, and the last
`cache_blocks` decoded blocks are kept. Decoding a whole ~65k-event block to return
one event is *right* here: sequential scrubbing is the dominant access pattern, so
the next request almost always lands in the same block, and the LRU makes that a
dictionary hit. Combined with `MAX_EVENTS_PER_BLOCK`, the retained memory is bounded
at `cache_blocks x MAX_EVENTS_PER_BLOCK` events even for a hostile file.
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

from chronotrace.recorder.events import Event
from chronotrace.store.columnar import MAX_EVENTS_PER_BLOCK, decode_events, peek_count
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
    BlockType,
    EocdFlag,
)
from chronotrace.store.errors import CorruptRecording, TruncatedRecording, UnsupportedVersion
from chronotrace.store.framing import BlockError, decode_block

DEFAULT_CACHE_BLOCKS = 8
_COUNT_SIZE = 4  # the u32 event count at the start of an EVENTS payload

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
        "_file",
        "_mmap",
        "_starts",
        "_truncated",
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
        self._blocks: list[_Block] = []
        self._starts: list[int] = []
        self._count = 0
        self._truncated = False
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

    # -- index: footer (lazy) or scan (crash recovery) ----------------------

    def _blocks_from_footer(self) -> tuple[list[tuple[int, int]], bool] | None:
        """EVENTS blocks (offset, count) from the footer index, or None.

        None when the footer is absent, out of bounds, corrupt, or claims an
        impossible count -- the caller then recovers by scanning.
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
            if entry_type != BlockType.EVENTS:
                continue
            count = self._safe_count(offset, length)
            if count is None:
                return None  # entry out of bounds or over the cap: distrust the footer
            locations.append((offset, count))
        return locations, bool(flags & EocdFlag.TRUNCATED)

    def _blocks_from_scan(self) -> list[tuple[int, int]]:
        """Recover EVENTS blocks by walking frames from the header, CRC-checking each.

        Stops at the first torn block. The crash path; it touches every page, unlike
        the footer path -- which is why it is the fallback, not the default.
        """
        buf, size = self._buf, len(self._buf)
        locations: list[tuple[int, int]] = []
        pos = HEADER_SIZE
        while pos < size:
            try:
                block_type, _flags, _payload, nxt = decode_block(buf, pos)
            except BlockError:
                break  # torn tail: the recovered prefix ends here
            if block_type == BlockType.EVENTS:
                count = self._safe_count(pos, nxt - pos)
                if count is None:
                    break  # a CRC-valid but impossible block: stop at the good prefix
                locations.append((pos, count))
            pos = nxt
        return locations

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
        if offset + BLOCK_HEADER_SIZE + _COUNT_SIZE > len(self._buf):
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
            _block_type, _flags, payload, _next = decode_block(self._buf, offset)  # CRC-checked
            events = decode_events(payload)  # allocations bounded (see columnar.py)
        except (BlockError, ValueError, struct.error) as exc:
            raise CorruptRecording(f"block at offset {offset}: {exc}") from exc
        self._cache[offset] = events
        if len(self._cache) > self._cache_blocks:
            self._cache.popitem(last=False)
        return events

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
