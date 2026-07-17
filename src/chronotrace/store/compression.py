"""zstd block compression, with decompression-bomb protection as the first rule.

Why zstd (measured, not by reputation -- benchmarks/RESULTS.md)
--------------------------------------------------------------
On a real columnar EVENTS block zstd-9 hits **285x at ~520 MB/s** compress and
**~3.6 GB/s** decompress. zlib-9 manages 90x; lzma reaches 525x but at **19 MB/s**
compress -- 27x slower, disqualifying on the recorder's hot path (EVENTS are
compressed on the traced program's own thread). zstd's decompress speed is the read
win the scrubber lives on. So zstd, default level 9 -- the knee where ratio has
climbed 6x over level 3 for a 2x speed cost, and level 19 buys nothing more.

**No trained dictionary.** A dictionary was measured (RESULTS.md) and *deleted*: it
is a net win only across many small, near-identical blocks, but a `.chrono`'s blocks
are large (65536 events) and self-contextualise through zstd's own window, so an
embedded 8 KB dictionary cost more than it saved on every real layout (a single
VALUES block: -7 KB net). It becomes worth revisiting only if day 18's block-size
sweep pushes blocks small; until then it is unearned complexity.

The bomb rule (this file parses untrusted input)
------------------------------------------------
A compressed block arrives in a stranger's bug report, so `decompress` NEVER
pre-allocates a size a frame declares without bounding it first. It reads the zstd
frame's *embedded content size* -- a header field, no decompression -- and refuses
before allocating if that exceeds the cap: an 80 MB bomb is rejected having touched
~1 KB. Only once the size is proven `<= cap` does it decompress one-shot. A frame that
declares *no* size is decompressed one-shot bounded by the cap itself -- zstd honours
`max_output_size` precisely when no size is embedded, so it stops rather than expand
past the cap. (Checking the embedded size first is essential: zstd honours it *ahead*
of `max_output_size`, so `max_output_size` alone is not a bound when a size is present.)

The raw fallback (a format that can only grow data is broken)
------------------------------------------------------------
When compression does not shrink a block -- already-random bytes: a digest table, an
encrypted value -- it is stored raw behind a `_RAW` codec byte, so the worst case is
`+5 bytes` of frame, never expansion.
"""

from __future__ import annotations

import struct

import zstandard as zstd

_RAW, _ZSTD = 0, 1
_FRAME = struct.Struct("<B I")  # codec (u8), raw_length (u32) -- prefixes every frame

DEFAULT_LEVEL = 9
"""zstd level. Measured knee (see module docstring): near-lzma ratio at 25x lzma's
compress speed, which the recorder hot path needs because it compresses EVENTS blocks
on the traced program's thread."""

MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
"""Hard ceiling on one block's decompressed size -- the anti-bomb bound. Above any
legitimate block (a 1M-event columnar payload is ~84 MB) and far below an OOM. No
frame decompresses past this no matter what it declares."""


class CompressionError(ValueError):
    """A compressed block is malformed or corrupt.

    A `ValueError` on purpose: the reader turns `BlockError`, `ValueError` and
    `struct.error` from a block into one `CorruptRecording`, so this needs no new
    clause there.
    """


class DecompressionBomb(CompressionError):
    """A block declares, or expands to, more than the caller's cap.

    Raised *before* the allocation it would cause, not after -- the entire point of
    treating compressed input from an untrusted file as hostile.
    """


def compress(data: bytes, *, level: int = DEFAULT_LEVEL) -> bytes:
    """Compress `data` into a self-describing frame, falling back to raw if it grows.

    Returns `[u8 codec][u32 raw_length][body]`. `raw_length` lets the reader size and
    verify the result; the codec byte says whether `body` is zstd or the raw fallback.
    """
    packed = zstd.ZstdCompressor(level=level).compress(data)
    if len(packed) >= len(data):
        return _FRAME.pack(_RAW, len(data)) + data
    return _FRAME.pack(_ZSTD, len(data)) + packed


def decompress(frame: bytes, *, max_output: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """Inverse of `compress`, bounded to at most `max_output` bytes.

    Raises:
        DecompressionBomb: the frame declares or expands past `max_output`.
        CompressionError: the frame is truncated, uses an unknown codec, or the
            compressed body is corrupt or does not match its declared `raw_length`.
    """
    if len(frame) < _FRAME.size:
        raise CompressionError("compressed frame shorter than its 5-byte header")
    codec, raw_len = _FRAME.unpack_from(frame, 0)
    if raw_len > max_output:
        raise DecompressionBomb(f"block declares {raw_len} bytes, over the {max_output} cap")
    body = frame[_FRAME.size :]
    if codec == _RAW:
        if len(body) != raw_len:
            raise CompressionError(f"raw block declares {raw_len} bytes, carries {len(body)}")
        return body
    if codec != _ZSTD:
        raise CompressionError(f"unknown compression codec {codec}")
    out = _zstd_decompress(body, raw_len)
    if len(out) != raw_len:
        raise CompressionError(f"decompressed to {len(out)} bytes, frame declared {raw_len}")
    return out


def _zstd_decompress(body: bytes, cap: int) -> bytes:
    """Decompress a zstd `body`, never allocating more than `cap` bytes.

    The frame's declared content size is a header field read without decompressing:
    reject it against `cap` before allocating, then decompress one-shot bounded by that
    size. A frame that declares *no* size (a spec-compliant writer may omit it, since
    the `raw_length` field already carries it) is decompressed one-shot bounded by `cap`
    itself -- zstd honours `max_output_size` precisely when no size is embedded, so it
    stops and errors rather than expand past the cap. Either way the bound holds before
    any large allocation.
    """
    try:
        declared = zstd.frame_content_size(body)
        if declared > cap:
            raise DecompressionBomb(f"frame declares {declared} bytes, over the {cap} cap")
        limit = declared if declared >= 0 else cap
        return bytes(zstd.ZstdDecompressor().decompress(body, max_output_size=limit))
    except zstd.ZstdError as exc:
        raise CompressionError(f"corrupt compressed block: {exc}") from exc
