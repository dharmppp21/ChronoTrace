"""Block framing: the one primitive every section of a `.chrono` file shares.

    [u32 payload_length][u16 block_type][u16 block_flags][u32 crc32][payload]

Encode and decode live in this one file, together, on purpose. Framing is the
single thing every section reuses, and a format rots when the frame is
re-implemented per section and the copies drift by a byte. There is one `encode`,
one `decode`, and a test that asserts they are inverses -- so a change to the
frame layout can only be made in one place and is caught if it is not symmetric.

Why length *and* CRC (spec §4)
------------------------------
The **length** frames the block: a reader can skip a block whose *type* it does
not understand by advancing past it, which is what makes the format
forward-compatible. The **CRC** proves the framed bytes are intact: a crash
mid-write leaves a final block whose length may look plausible but whose payload
is short or garbage, and the CRC turns that into a detected torn write instead of
data. Neither replaces the other.

A `decode` follows two rules from the spec, in order: it checks the claimed length
against the bytes actually present *before* reading (a crash can write a garbage
length that would otherwise allocate gigabytes), and it verifies the CRC *before*
returning the payload (never hand back unverified bytes). Both failures raise
`BlockError`, which a reader treats as the end of the valid prefix.
"""

from __future__ import annotations

import zlib

from chronotrace.store.constants import BLOCK_HEADER, BLOCK_HEADER_SIZE, BlockFlag, BlockType

_U32_MAX = 0xFFFFFFFF


class BlockError(ValueError):
    """A frame is malformed or corrupt: too short, overruns, or fails its CRC.

    Not a bug in the reader -- the expected signal of a torn write, which the
    recovery scan uses to find the end of the readable prefix.
    """


def encode_block(block_type: BlockType, payload: bytes, flags: BlockFlag = BlockFlag.NONE) -> bytes:
    """Frame `payload` as a block.

    Args:
        block_type: the section this block carries.
        payload: the bytes to frame, already in their final on-disk form (e.g.
            already compressed, if `flags` says so) -- the CRC covers exactly these.
        flags: how the payload is stored.

    Returns:
        The 12-byte frame followed by the payload.

    Raises:
        ValueError: the payload is larger than a u32 length can describe. A block
            is a bounded batch of events, so this is a caller bug, not a torn file.

    Complexity: O(len(payload)) for the CRC.
    """
    if len(payload) > _U32_MAX:
        raise ValueError(f"block payload of {len(payload)} bytes exceeds the u32 length limit")
    return BLOCK_HEADER.pack(len(payload), block_type, flags, zlib.crc32(payload)) + payload


def decode_block(data: bytes, offset: int = 0) -> tuple[int, int, bytes, int]:
    """Decode the block at `offset` in `data`.

    Args:
        data: a buffer (bytes or mmap) containing at least one block at `offset`.
        offset: where the block's frame begins.

    Returns:
        `(block_type, flags, payload, next_offset)` -- `next_offset` is where the
        following block would begin, so a caller can walk blocks in a loop.

    Raises:
        BlockError: the frame is truncated, the length overruns the buffer, or the
            CRC does not match. Every one of these means "stop; the rest is torn".

    Complexity: O(len(payload)) for the CRC.
    """
    if offset + BLOCK_HEADER_SIZE > len(data):
        raise BlockError("truncated block header")
    length, block_type, flags, crc = BLOCK_HEADER.unpack_from(data, offset)
    start = offset + BLOCK_HEADER_SIZE
    end = start + length
    if end > len(data):
        raise BlockError(f"block length {length} overruns the buffer of {len(data)} bytes")
    payload = bytes(data[start:end])
    if zlib.crc32(payload) != crc:
        raise BlockError("block CRC mismatch: torn write or corruption")
    return block_type, flags, payload, end
