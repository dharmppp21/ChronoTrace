"""Scan recovery, precise torn-tail classification, and repair's never-in-place rule.

The real-process kill test lives in test_crash_real.py; this file drives the recovery
machinery with synthetic truncations and forged tails, deterministically."""

from __future__ import annotations

import io
import tracemalloc
from pathlib import Path

import pytest

from chronotrace.recorder.events import Event, EventKind
from chronotrace.store import ChronoReader, CorruptRecording, TruncatedRecording, repair
from chronotrace.store.constants import BLOCK_HEADER, HEADER_SIZE, BlockType
from chronotrace.store.recovery import TailStatus, has_valid_footer, walk_blocks
from chronotrace.store.writer import ChronoWriter


def _ev(seq: int) -> Event:
    return Event(
        seq=seq,
        kind=EventKind.LINE,
        timestamp_ns=1000 + seq,
        thread_id=1,
        frame_id=1,
        code_id=1,
        lineno=seq % 7,
    )


def _valid_file(n: int, *, block_events: int = 4, keyframe_interval: int = 1000) -> bytes:
    buf = io.BytesIO()
    writer = ChronoWriter(buf, block_events=block_events, keyframe_interval=keyframe_interval)
    for s in range(n):
        writer.add(_ev(s))
    writer.close()
    return buf.getvalue()


def _read(data: bytes) -> tuple[list[int], bool]:
    reader = ChronoReader.from_bytes(data)
    return [e.seq for e in reader.iter_events()], reader.truncated


# ---------------------------------------------------------------------------
# Torn-tail classification
# ---------------------------------------------------------------------------


def test_a_footerless_file_ending_on_a_block_boundary_is_clean() -> None:
    full = _valid_file(20)
    blocks, _ = walk_blocks(full)
    boundary = blocks[len(blocks) // 2][3]  # the next-offset of a mid-file block
    _blocks, status = walk_blocks(full[:boundary])
    assert status is TailStatus.CLEAN  # cut exactly at a boundary: nothing torn


def test_a_partial_final_block_is_classified_partial() -> None:
    full = _valid_file(20)
    blocks, _ = walk_blocks(full)
    # Cut a few bytes into the last data block: its length now reads past EOF.
    cut = blocks[-1][0] + 4
    _blocks, status = walk_blocks(full[:cut])
    assert status is TailStatus.TRUNCATED_PARTIAL


def test_a_complete_block_with_a_bad_crc_is_classified_corrupt() -> None:
    full = bytearray(_valid_file(20))
    blocks, _ = walk_blocks(bytes(full))
    offset, _bt, _fl, nxt = blocks[-2]  # a full, non-final block
    truncated = full[:nxt]  # drop everything after it so it is the tail
    truncated[offset + 12] ^= 0xFF  # corrupt a payload byte -> CRC fails
    _blocks, status = walk_blocks(bytes(truncated))
    assert status is TailStatus.TRUNCATED_CORRUPT


def test_a_torn_length_field_is_bounded_never_allocated() -> None:
    """A tail whose length field reads as 2 GB must be classified, not allocated."""
    forged = _valid_file(8)[:HEADER_SIZE]  # header, then a forged frame
    forged += BLOCK_HEADER.pack(2**31, BlockType.EVENTS, 0, 0) + b"tiny"
    tracemalloc.start()
    blocks, status = walk_blocks(forged)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert status is TailStatus.TRUNCATED_PARTIAL  # length past EOF
    assert blocks == []  # nothing before it to recover
    assert peak < 1_000_000, "a torn length field was allocated"


# ---------------------------------------------------------------------------
# Recovery through the reader
# ---------------------------------------------------------------------------


def test_scan_recovers_a_clean_seq_dense_prefix() -> None:
    full = _valid_file(40)
    for cut in (len(full) // 3, len(full) // 2, len(full) * 3 // 4):
        seqs, truncated = _read(full[:cut])
        assert seqs == list(range(len(seqs))), f"cut={cut}: not a clean prefix"
        assert truncated is True


def test_a_partial_block_is_discarded_not_partially_decoded() -> None:
    full = _valid_file(40)  # block_events=4 -> ten EVENTS blocks
    blocks, _ = walk_blocks(full)
    last_events = [b for b in blocks if b[1] == BlockType.EVENTS][-1]
    seqs, _ = _read(full[: last_events[0] + 6])  # cut partway into the last EVENTS block
    # A block is all-or-nothing (its CRC covers the whole payload): the half-written
    # block yields *none* of its events, never a partial subset of invented ones.
    assert len(seqs) < 40
    assert len(seqs) % 4 == 0  # only whole blocks survived
    assert seqs == list(range(len(seqs)))  # a clean prefix


def test_crash_between_header_and_first_block_opens_empty() -> None:
    seqs, truncated = _read(_valid_file(20)[:HEADER_SIZE])
    assert seqs == []
    assert truncated is True


def test_crash_mid_header_is_truncated_recording() -> None:
    with pytest.raises(TruncatedRecording):
        ChronoReader.from_bytes(_valid_file(20)[: HEADER_SIZE - 5])


def test_a_keyframe_lost_in_the_tail_still_finds_an_earlier_one() -> None:
    full = _valid_file(100, block_events=8, keyframe_interval=20)  # keyframes 0,20,40,60,80
    reader = ChronoReader.from_bytes(full[: len(full) * 3 // 5])  # lose the last keyframes
    assert reader.truncated is True
    last = len(reader) - 1
    kf = reader.nearest_keyframe_at_or_before(last)
    assert kf is not None and kf.seq <= last  # an earlier surviving keyframe, never None


# ---------------------------------------------------------------------------
# repair: rebuild the footer, never destroy the original
# ---------------------------------------------------------------------------


def test_repair_rebuilds_a_footer_and_keeps_the_truncated_flag(tmp_path: Path) -> None:
    path = tmp_path / "rec.chrono"
    crashed = _valid_file(40)
    path.write_bytes(crashed[: len(crashed) * 2 // 3])  # no footer
    assert not has_valid_footer(path.read_bytes())

    repair(path)
    repaired = path.read_bytes()
    assert has_valid_footer(repaired)  # now opens O(1)
    reader = ChronoReader.from_bytes(repaired)
    assert reader.truncated is True  # ...but is still an incomplete recording


def test_repair_is_idempotent_on_an_intact_file(tmp_path: Path) -> None:
    path = tmp_path / "rec.chrono"
    path.write_bytes(_valid_file(40))
    before = path.read_bytes()
    repair(path)
    assert path.read_bytes() == before  # a valid file is passed through untouched


def test_repair_to_out_leaves_the_original_untouched(tmp_path: Path) -> None:
    src = tmp_path / "src.chrono"
    out = tmp_path / "out.chrono"
    crashed = _valid_file(40)
    src_bytes = crashed[: len(crashed) * 2 // 3]
    src.write_bytes(src_bytes)

    repair(src, out)
    assert src.read_bytes() == src_bytes  # original byte-for-byte unchanged
    assert has_valid_footer(out.read_bytes())  # the repaired copy is valid


def test_repair_on_a_non_chrono_file_raises_and_writes_nothing(tmp_path: Path) -> None:
    src = tmp_path / "junk.chrono"
    src.write_bytes(b"this is not a chrono file, definitely not, at all, nope nope")
    with pytest.raises(CorruptRecording):
        repair(src)
    # The header check fails before any write, so no temp debris is left behind.
    assert [p.name for p in tmp_path.iterdir()] == ["junk.chrono"]
