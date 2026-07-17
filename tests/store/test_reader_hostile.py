"""The reader is a trust boundary: a `.chrono` from a stranger's bug report must
never crash, hang, or OOM this process. These tests attack it.

The truncation sweep is the single most important test in the storage layer: it
truncates a valid file at *every* byte offset and asserts each case either opens
with a valid prefix or raises a precise `ChronoError` -- nothing else.
"""

from __future__ import annotations

import contextlib
import io
import struct
import tracemalloc

import pytest

from chronotrace.recorder.events import Event, EventKind
from chronotrace.store import ChronoError, ChronoReader, CorruptRecording
from chronotrace.store.constants import (
    BLOCK_HEADER,
    FORMAT_VERSION_MAJOR,
    FORMAT_VERSION_MINOR,
    HEADER,
    HEADER_SIZE,
    MAGIC,
    BlockFlag,
    BlockType,
)
from chronotrace.store.framing import encode_block


def _ev(seq: int) -> Event:
    return Event(
        seq=seq,
        kind=EventKind.LINE,
        timestamp_ns=1000 + seq,
        thread_id=1,
        frame_id=1,
        code_id=1,
        lineno=seq % 5,
    )


def _valid_file(n: int, block_events: int) -> bytes:
    from chronotrace.store.writer import ChronoWriter

    buf = io.BytesIO()
    writer = ChronoWriter(buf, block_events=block_events)
    for s in range(n):
        writer.add(_ev(s))
    writer.close()
    return buf.getvalue()


def _header() -> bytes:
    return HEADER.pack(MAGIC, FORMAT_VERSION_MAJOR, FORMAT_VERSION_MINOR, 0, HEADER_SIZE)


def _read_all(data: bytes) -> list[Event]:
    return list(ChronoReader.from_bytes(data).iter_events())


# ---------------------------------------------------------------------------
# The truncation sweep
# ---------------------------------------------------------------------------


def test_truncation_at_every_offset_opens_a_prefix_or_raises_cleanly() -> None:
    full = _valid_file(20, block_events=4)  # ~5 EVENTS blocks, a few hundred bytes
    for cut in range(len(full) + 1):
        chopped = full[:cut]
        try:
            events = _read_all(chopped)
        except ChronoError:
            continue  # a precise error is a valid outcome
        # Opened: the events must be a clean seq-dense prefix, never a partial event.
        assert [e.seq for e in events] == list(range(len(events))), f"cut={cut}"


def test_bit_flips_are_caught_or_absorbed() -> None:
    full = bytearray(_valid_file(40, block_events=8))
    for pos in range(0, len(full), 3):  # sample every 3rd byte
        flipped = bytearray(full)
        flipped[pos] ^= 0x01
        try:
            events = _read_all(bytes(flipped))
        except ChronoError:
            continue
        # If it still opened, whatever it returned is a valid seq-dense prefix.
        assert [e.seq for e in events] == list(range(len(events))), f"pos={pos}"


# ---------------------------------------------------------------------------
# Amplification attacks: a small file must not request a huge allocation
# ---------------------------------------------------------------------------


def _assert_bounded(fn: object) -> None:
    """Run `fn` under a 10 MB Python-allocation ceiling, swallowing ChronoError.

    If a giant length in the file were trusted, the allocation would blow the
    ceiling and fail the assertion.
    """
    tracemalloc.start()
    with contextlib.suppress(ChronoError):
        fn()  # type: ignore[operator]
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 10_000_000, f"decode allocated {peak} bytes -- a length was trusted"


def test_a_4gb_block_length_is_a_clean_error_not_an_oom() -> None:
    forged = _header() + BLOCK_HEADER.pack(2**31, BlockType.EVENTS, BlockFlag.NONE, 0) + b"tiny"
    _assert_bounded(lambda: _read_all(forged))
    assert _read_all(forged) == []  # the overrun block is rejected; nothing recovered


def test_a_block_claiming_a_billion_events_is_rejected() -> None:
    payload = struct.pack("<I", 2**30)  # event_count way over MAX_EVENTS_PER_BLOCK
    forged = _header() + encode_block(BlockType.EVENTS, payload)  # valid CRC over a hostile count
    _assert_bounded(lambda: _read_all(forged))
    assert _read_all(forged) == []  # count over the cap: block not indexed


def test_a_giant_rle_run_is_bounded() -> None:
    """A CRC-valid block whose first column claims a 2**40-long RLE run."""
    payload = (
        struct.pack("<I", 5)  # a modest, in-cap event count
        + struct.pack("<BI", 1, 16)  # column 0 (seq): codec=RLE, 16 bytes
        + struct.pack("<2q", 0, 2**40)  # one pair: value 0, run 2**40 -- the attack
    )
    forged = _header() + encode_block(BlockType.EVENTS, payload)
    reader = ChronoReader.from_bytes(forged)
    _assert_bounded(lambda: reader[0])
    with pytest.raises(CorruptRecording):
        reader[0]


def test_a_compressed_block_declaring_a_huge_size_is_a_clean_error_not_an_oom() -> None:
    """A decompression bomb: the block indexes fine (its raw u32 count is a modest 1),
    but the compression frame behind the count claims a 2 GB decompressed size. The
    reader must reject it on access without allocating the 2 GB."""
    count = struct.pack("<I", 1)  # peek_count sees a valid count, so the block is indexed
    frame = struct.pack("<B I", 1, 2**31) + b"x"  # codec=zstd, raw_len=2GB, 1-byte body
    forged = _header() + encode_block(BlockType.EVENTS, count + frame, BlockFlag.COMPRESSED_ZSTD)
    reader = ChronoReader.from_bytes(forged)
    _assert_bounded(lambda: list(reader.iter_events()))
    with pytest.raises(CorruptRecording):
        list(reader.iter_events())


def test_a_footer_pointing_into_the_header_falls_back_to_scan() -> None:
    """An index offset that points backwards must not be dereferenced blindly."""
    from chronotrace.store.constants import EOCD, EOCD_MAGIC

    bad_footer = EOCD.pack(5, 10, 0, 0, EOCD_MAGIC)  # index_offset=5, inside the header
    forged = _header() + bad_footer
    events = _read_all(forged)  # must not crash; scan finds nothing valid
    assert events == []


def test_pure_garbage_after_a_valid_header_recovers_nothing() -> None:
    forged = _header() + bytes(range(256)) * 4  # valid header, then noise
    assert _read_all(forged) == []  # scan stops at the first non-frame; no crash
