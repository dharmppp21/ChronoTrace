"""Framing is the primitive everything else trusts, so it is tested as inverses
and against every torn-write shape a crash can produce."""

from __future__ import annotations

from typing import Any

import pytest

from chronotrace.store.constants import BLOCK_HEADER, BlockFlag, BlockType
from chronotrace.store.framing import BlockError, decode_block, encode_block


def test_encode_decode_round_trips() -> None:
    block = encode_block(BlockType.EVENTS, b"payload-bytes", BlockFlag.NONE)
    block_type, flags, payload, nxt = decode_block(block)
    assert block_type == BlockType.EVENTS
    assert flags == BlockFlag.NONE
    assert payload == b"payload-bytes"
    assert nxt == len(block)


def test_empty_payload_round_trips() -> None:
    """A zero-length payload is valid (an empty section) and CRC-covered."""
    block = encode_block(BlockType.META, b"")
    block_type, _flags, payload, nxt = decode_block(block)
    assert block_type == BlockType.META
    assert payload == b""
    assert nxt == len(block)


def test_next_offset_lets_blocks_be_walked() -> None:
    two = encode_block(BlockType.META, b"first") + encode_block(BlockType.EVENTS, b"second!")
    t0, _f0, p0, nxt = decode_block(two, 0)
    t1, _f1, p1, end = decode_block(two, nxt)
    assert (t0, p0) == (BlockType.META, b"first")
    assert (t1, p1) == (BlockType.EVENTS, b"second!")
    assert end == len(two)


def test_a_single_flipped_bit_is_detected() -> None:
    block = bytearray(encode_block(BlockType.EVENTS, b"important"))
    block[-1] ^= 0x01  # corrupt one payload byte
    with pytest.raises(BlockError, match="CRC"):
        decode_block(bytes(block))


def test_a_length_that_overruns_the_buffer_is_rejected() -> None:
    """A crash can write a plausible length pointing past the data. Never read it."""
    forged = BLOCK_HEADER.pack(1000, BlockType.EVENTS, BlockFlag.NONE, 0) + b"only-a-few"
    with pytest.raises(BlockError, match="overruns"):
        decode_block(forged)


def test_a_truncated_frame_is_rejected() -> None:
    with pytest.raises(BlockError, match="truncated"):
        decode_block(b"\x01\x02\x03")  # fewer than 12 header bytes


def test_payload_over_the_length_limit_is_rejected(monkeypatch: Any) -> None:
    """A payload too big for a u32 length is a caller bug. Tested by shrinking the
    limit rather than allocating 4 GiB to trip the real one."""
    monkeypatch.setattr("chronotrace.store.framing._U32_MAX", 4)
    with pytest.raises(ValueError, match="u32 length"):
        encode_block(BlockType.EVENTS, b"too-long")
