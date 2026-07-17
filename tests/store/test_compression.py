"""Compression round-trips, the raw fallback, and -- the point of the file -- that a
hostile compressed frame can never OOM: it is refused before the allocation, and the
streaming decompressor caps a real zip-bomb's memory."""

from __future__ import annotations

import os
import struct
import tracemalloc

import pytest
import zstandard as zstd

from chronotrace.store.compression import (
    MAX_DECOMPRESSED_BYTES,
    CompressionError,
    DecompressionBomb,
    compress,
    decompress,
)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"x",
        b"the same shape of msgpack map, over and over" * 500,  # very compressible
        bytes(range(256)) * 1000,  # structured
    ],
    ids=["empty", "one-byte", "repetitive", "structured"],  # short ids: a bytes value
    # in the id overflows PYTEST_CURRENT_TEST (a 32 KB env-var limit) on Windows.
)
def test_round_trip(data: bytes) -> None:
    assert decompress(compress(data)) == data


def test_compressible_data_shrinks() -> None:
    data = b"region north sales orders path /srv/app/module.py " * 1000
    frame = compress(data)
    assert len(frame) < len(data)
    assert decompress(frame) == data


def test_incompressible_data_falls_back_to_raw_never_grows() -> None:
    """Random bytes cannot be compressed; the frame must not be larger than data + header."""
    data = os.urandom(4096)
    frame = compress(data)
    assert len(frame) <= len(data) + 5  # a codec byte + a u32 length, nothing more
    assert decompress(frame) == data


def test_a_frame_declaring_a_huge_size_is_rejected_before_allocating() -> None:
    """The bomb: a tiny frame that claims 2 GB. Rejected on the declared length alone."""
    forged = struct.pack("<B I", 1, 2_000_000_000) + b"tiny"  # codec=zstd, raw_len=2GB
    tracemalloc.start()
    with pytest.raises(DecompressionBomb):
        decompress(forged, max_output=MAX_DECOMPRESSED_BYTES)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 5_000_000, f"rejected but still allocated {peak} bytes"


def test_a_real_bomb_with_an_honest_declared_size_is_rejected_before_allocating() -> None:
    """80 MB of zeros compresses to a few KB but the frame declares 80 MB. A caller with
    a 1 MB cap must reject it on the declared size alone, allocating nothing."""
    bomb = compress(b"\x00" * (80 * 1024 * 1024))  # honest frame: raw_len = 80 MB
    tracemalloc.start()
    with pytest.raises(DecompressionBomb):
        decompress(bomb, max_output=1 * 1024 * 1024)  # 1 MB cap vs an 80 MB payload
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 5_000_000, f"rejected but still allocated {peak} bytes"


def test_a_size_less_frame_is_still_bounded_by_the_cap() -> None:
    """A frame can omit its content size (a spec-compliant writer may, since raw_length
    carries it). Decompression must still cap memory: the 80 MB expands past the frame's
    modest declared raw_length, so zstd stops at the cap and errors rather than allocate
    it all."""
    sizeless = zstd.ZstdCompressor(level=9, write_content_size=False).compress(
        b"\x00" * (80 * 1024 * 1024)
    )
    assert zstd.frame_content_size(sizeless) < 0  # genuinely has no declared size
    forged = struct.pack("<B I", 1, 1_000_000) + sizeless  # codec=zstd, raw_len=1 MB
    tracemalloc.start()
    with pytest.raises(CompressionError):  # zstd stops at the cap: "did not decompress full frame"
        decompress(forged, max_output=MAX_DECOMPRESSED_BYTES)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 5_000_000, f"cap failed: held {peak} bytes"


def test_corrupt_compressed_body_is_a_clean_error() -> None:
    frame = bytearray(compress(b"hello world " * 100))
    frame[10] ^= 0xFF  # flip a byte in the zstd stream
    with pytest.raises(CompressionError):
        decompress(bytes(frame))


def test_unknown_codec_is_rejected() -> None:
    forged = struct.pack("<B I", 99, 4) + b"data"
    with pytest.raises(CompressionError):
        decompress(forged)


def test_truncated_frame_header_is_rejected() -> None:
    with pytest.raises(CompressionError):
        decompress(b"\x01\x00")  # shorter than the 5-byte header


def test_raw_frame_with_a_lying_length_is_rejected() -> None:
    forged = struct.pack("<B I", 0, 999) + b"short"  # codec=raw, claims 999, carries 5
    with pytest.raises(CompressionError):
        decompress(forged)
