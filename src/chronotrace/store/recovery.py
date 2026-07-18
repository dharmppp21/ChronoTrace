"""Recover a `.chrono` file from a process killed mid-write, and prove the guarantee.

A debugger records programs that **crash** -- that is the whole point. A recording
readable only after a clean exit is useless exactly when it matters. ADR-0004 promises:
*every block whose length and CRC validate is readable; the tail may be lost; the prefix
is never corrupt.* This file makes that true.

Why this works without `fsync` (the argument that ties the design together)
--------------------------------------------------------------------------
The writer never `fsync`s per block (it would gate the traced program on disk latency).
It does `flush` each block to the OS, so completed blocks live in the page cache. A
`kill -9`/`TerminateProcess` kills the *process* but not the kernel's page cache, so
those blocks survive and this scanner recovers them. Only a power loss or kernel panic
can drop the unflushed tail -- and losing the last few events of a debug artifact is an
acceptable price for not halving the traced program's speed. Framing + per-block flush
+ this scanner is the durability story; fsync is the part we deliberately skip.

The four tail classifications (`walk_blocks` returns which)
---------------------------------------------------------
Walking blocks from the header, one of four things ends the walk:

* **CLEAN** -- the walk reached the exact end of the last block. Nothing was torn.
* **TRUNCATED_PARTIAL** -- the tail is too short for the frame it starts: either fewer
  than a 12-byte header remain, or the header's length field reads past EOF. The block
  was half-written. It is *discarded*, never partially decoded.
* **TRUNCATED_CORRUPT** -- a full block is present but its CRC fails: a torn payload, or
  garbage. Also discarded.

Discard, never salvage: half a block cannot be trusted, because you cannot know which
fields survived, and a debugger that shows *invented* state is worse than one that shows
less. The valid prefix is always what is returned, and the recording is flagged
truncated so the user is never told the program ended where the file happens to.
"""

from __future__ import annotations

import enum
import mmap
import os
import tempfile
import zlib
from pathlib import Path

from chronotrace.store.constants import (
    BLOCK_HEADER,
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
from chronotrace.store.framing import BlockError, decode_block, encode_block

# (offset, block_type, flags, next_offset) for one intact block.
BlockLoc = tuple[int, int, int, int]


class TailStatus(enum.Enum):
    """Why a block walk ended.

    `CLEAN` means nothing was torn; the others mean the tail was lost and the recording
    is a valid prefix flagged truncated.
    """

    CLEAN = "clean"
    TRUNCATED_PARTIAL = "truncated_partial"  # the tail is shorter than the frame it starts
    TRUNCATED_CORRUPT = "truncated_corrupt"  # a full block is present but its CRC fails


def walk_blocks(buf: bytes | mmap.mmap) -> tuple[list[BlockLoc], TailStatus]:
    """Walk intact blocks from the header until the tail, CRC-checking each.

    The recovery primitive, used by the reader when the footer is absent and by
    `repair`. O(file size) -- it touches every block, unlike an O(1) footer open -- which
    is the right trade for a *rare* path: a crashed recording has no footer to be fast
    with, and correctness (never trusting a torn block) beats speed here.

    Returns the intact block locations and why the walk stopped.
    """
    blocks: list[BlockLoc] = []
    size = len(buf)
    pos = HEADER_SIZE
    while pos < size:
        try:
            block_type, flags, _payload, nxt = decode_block(buf, pos)  # CRC-checked
        except BlockError:
            return blocks, classify_tail(buf, pos)
        blocks.append((pos, block_type, flags, nxt))
        pos = nxt
    return blocks, TailStatus.CLEAN


def classify_tail(buf: bytes | mmap.mmap, pos: int) -> TailStatus:
    """Classify the torn tail beginning at `pos`. See the module docstring.

    A torn *length field* is handled here without ever allocating it: the declared
    length is only compared against the bytes that remain, never used to read.
    """
    size = len(buf)
    if pos + BLOCK_HEADER_SIZE > size:
        return TailStatus.TRUNCATED_PARTIAL  # not even a full 12-byte frame header remains
    length = BLOCK_HEADER.unpack_from(buf, pos)[0]
    if pos + BLOCK_HEADER_SIZE + length > size:
        return TailStatus.TRUNCATED_PARTIAL  # the length reads past EOF: block half-written
    return TailStatus.TRUNCATED_CORRUPT  # full block present, CRC failed: torn payload or garbage


def has_valid_footer(data: bytes) -> bool:
    """Whether `data` already ends in a clean footer (so `repair` is a no-op on it)."""
    size = len(data)
    if size < HEADER_SIZE + EOCD_SIZE:
        return False
    index_offset, _len, index_crc, _flags, magic = EOCD.unpack_from(data, size - EOCD_SIZE)
    if magic != EOCD_MAGIC or not HEADER_SIZE <= index_offset <= size - EOCD_SIZE:
        return False
    try:
        block_type, _flags, payload, _next = decode_block(data, index_offset)
    except BlockError:
        return False
    return block_type == BlockType.INDEX and zlib.crc32(payload) == index_crc


def repair(src: str | os.PathLike[str], dst: str | os.PathLike[str] | None = None) -> Path:
    """Rebuild a footer for a crashed recording so later opens are O(1) again.

    **Never modifies the original in place.** The repaired bytes are written to a temp
    file, fsynced, and `os.replace`d onto `dst` atomically -- so a repair interrupted at
    any point leaves the original (often the only copy of an irreproducible bug) intact.
    `dst` defaults to `src` (an atomic swap). Idempotent: a file that already has a valid
    footer is passed through unchanged.

    Raises:
        CorruptRecording / UnsupportedVersion / TruncatedRecording: the source is not a
            readable `.chrono` file to begin with.
    """
    src_path = Path(src)
    dst_path = Path(dst) if dst is not None else src_path
    data = src_path.read_bytes()
    _check_header(data)
    if has_valid_footer(data):
        if dst_path != src_path:
            _atomic_write(dst_path, data)
        return dst_path
    blocks, _status = walk_blocks(data)
    prefix_end = blocks[-1][3] if blocks else HEADER_SIZE
    _atomic_write(dst_path, data[:prefix_end] + _footer(blocks, prefix_end))
    return dst_path


def _check_header(data: bytes) -> None:
    if len(data) < HEADER_SIZE:
        raise TruncatedRecording(f"file is {len(data)} bytes, smaller than a header")
    magic, major, minor, _flags, _hsize = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise CorruptRecording("not a .chrono file: bad magic")
    if major > FORMAT_VERSION_MAJOR:
        raise UnsupportedVersion(f"file is format v{major}.{minor}; upgrade ChronoTrace")


def _footer(blocks: list[BlockLoc], index_offset: int) -> bytes:
    """An INDEX block over `blocks` plus a TRUNCATED-flagged EOCD.

    The repaired file keeps the truncated flag: it now opens fast, but the recording is
    still an incomplete prefix and the user must keep seeing that.
    """
    entries = b"".join(
        INDEX_ENTRY.pack(block_type, offset, nxt - offset)
        for offset, block_type, _flags, nxt in blocks
    )
    index_block = encode_block(BlockType.INDEX, entries)
    eocd = EOCD.pack(
        index_offset, len(index_block), zlib.crc32(entries), EocdFlag.TRUNCATED, EOCD_MAGIC
    )
    return index_block + eocd


def _atomic_write(dst: Path, content: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=dst.name, suffix=".repair")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())  # the repaired file must itself be durable before the swap
        tmp.replace(dst)  # atomic: dst is the old file or the new one, never a mix
    except BaseException:
        tmp.unlink(missing_ok=True)  # a failed repair leaves no debris and never touches dst
        raise
