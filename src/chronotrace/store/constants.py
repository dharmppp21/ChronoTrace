"""The `.chrono` format's fixed constants -- the single source of truth.

Every magic byte, struct layout, version number and block-type tag the format
defines lives here and nowhere else. The writer (day 12) and any future reader --
including one written in another language from `docs/format-spec.md` -- must agree
with this file to the byte. The normative prose is the spec; this is its machine
form, and `tests/store/test_constants.py` pins the byte layout so an accidental
edit breaks CI loudly rather than silently changing a format that recordings in
the wild depend on.

There is no logic here on purpose. Encoding and decoding (with their validation,
CRC checks and crash recovery) are day 12; putting them here would make the
"constants" file a place decisions hide. This file only *declares*.

Endianness is fixed
-------------------
Every integer in a `.chrono` file is **little-endian**, always, regardless of the
host that wrote or reads it. x86 and ARM are little-endian, so the common path
pays nothing; a big-endian host converts. The format never carries a byte-order
mark and a reader never guesses -- "never rely on the host" means the format, not
the machine, decides. This is why every `struct` format string below starts `<`.
"""

from __future__ import annotations

import enum
import struct

MAGIC = b"\x89CHRONO\r\n\x1a\n"
"""File signature, 11 bytes, at offset 0. Modelled on PNG's:

* `\\x89` -- high bit set, so the file is not valid UTF-8 and not mistaken for text.
* `CHRONO` -- human-visible in a hexdump.
* `\\r\\n` ... `\\n` with `\\x1a` between -- the classic canary: catches a transfer
  that translated line endings (CRLF<->LF) or truncated at a DOS EOF (`\\x1a`).

Not any registered file signature. `.chrono` files are marked `binary` in
`.gitattributes`, but a recording shared by email or an old FTP client can still
be mangled, and this makes that corruption a loud failure at open, not silent
misread data.
"""

EOCD_MAGIC = b"CHRONEND"
"""End-of-central-directory signature, 8 bytes, at the very end of a cleanly
closed file. Its *presence at a fixed offset from EOF* is the "closed cleanly"
signal; its absence means the writer was killed mid-recording and the reader must
recover by scanning blocks. Positional, so unlike `MAGIC` it need not be
non-UTF-8."""

FORMAT_VERSION_MAJOR = 1
"""Incompatible-change counter. A reader MUST refuse a file whose major exceeds
its own -- the layout may have changed in ways it cannot parse."""

FORMAT_VERSION_MINOR = 3
"""Backward-compatible-addition counter. A reader MAY open a file with a higher
minor than its own: new *optional* blocks are skippable and new header fields live
past `header_size`. It MUST NOT guess at anything it does not recognise.

* 1 (day 14) activated the `COMPRESSED_ZSTD` block flag (reserved in 1.0) and made
  the VALUES section real msgpack.
* 2 (day 15) added the optional `KEYFRAMES` block: periodic snapshots of live state.
* 3 (day 16) added the optional `DELTAS` block: invertible state transitions between
  keyframes. Optional again, so a reader that predates it skips the block and can
  still reconstruct forward from the events -- a minor bump, not a major.

A current reader reads every earlier file unchanged; an older reader opening a newer
file skips the optional blocks it does not know and loses only fast seek, not data."""

# ---------------------------------------------------------------------------
# Fixed structures. Field order is the on-disk order; `<` is little-endian.
# ---------------------------------------------------------------------------

HEADER = struct.Struct("<11s H H Q H 7x")
"""File header at offset 0, 32 bytes. Fields, in order:

    magic            11s  MAGIC
    version_major    H    u16
    version_minor    H    u16
    flags            Q    u64  reserved file-wide feature bitfield; 0 in 1.0. A
                               reader MUST refuse a file that sets a bit it does
                               not understand -- a file-wide flag is not skippable.
    header_size      H    u16  = HEADER_SIZE; where the first block begins
    (padding)        7x   reserved, zero

`header_size` is stored so a future minor version can append fixed fields: an old
reader trusts `header_size` to find the body and ignores header bytes it does not
know. Padded to 32 for mmap-friendly alignment and room to grow without moving the
body.
"""
HEADER_SIZE = HEADER.size  # 32; derived so the size can never drift from the layout

BLOCK_HEADER = struct.Struct("<I H H I")
"""Per-block frame, 12 bytes, immediately before each payload. Fields, in order:

    payload_length   I    u32  bytes of payload that follow (post-compression)
    block_type       H    u16  BlockType
    block_flags      H    u16  BlockFlag (e.g. which codec compressed the payload)
    payload_crc32    I    u32  CRC-32 of the payload bytes as stored on disk

Both length and CRC are load-bearing and neither replaces the other: the length
frames the block so a reader can skip a block whose *type* it does not understand,
and the CRC proves the framed bytes are intact so a torn final write is detected
rather than interpreted as data. A reader MUST check `payload_length` against the
bytes remaining in the file *before* reading, and MUST verify the CRC *before*
trusting the payload -- a crash can leave a plausible length pointing at garbage.
"""
BLOCK_HEADER_SIZE = BLOCK_HEADER.size  # 12

EOCD = struct.Struct("<Q Q I I 8s")
"""End-of-central-directory record, 32 bytes, at the very end of a closed file.

    index_offset     Q    u64  file offset of the INDEX block's header
    index_length     Q    u64  total bytes of the INDEX block (header + payload)
    index_crc32      I    u32  CRC-32 of the INDEX block's payload (redundant
                               with the block's own CRC; lets a reader trust the
                               pointer before seeking)
    flags            I    u32  EocdFlag; TRUNCATED means events were dropped
    magic            8s   EOCD_MAGIC

Offsets are u64: a `.chrono` file may be far larger than 4 GiB (ADR-0001 makes
recordings big), so nothing in the index may be a 32-bit offset. Per-block
*lengths* stay u32 because a block is a bounded batch of events, never gigabytes.

`flags` gained a field on day 12, when implementing the writer showed that a
*cleanly closed but incomplete* recording (events dropped under backpressure) is
distinct from a crash (no EOCD at all) and from a complete one -- and the reader
must be able to tell the UI which. It is informational, never gating: a reader
that does not know a `flags` bit still reads the file (unlike a header `flags` bit).
Total event count is deliberately *not* stored here -- it is the sum of the EVENTS
blocks' own counts, and the format does not store what it can derive.
"""
EOCD_SIZE = EOCD.size  # 32


class EocdFlag(enum.IntFlag):
    """The EOCD `flags` bitfield. Informational; a reader never refuses on these."""

    NONE = 0
    TRUNCATED = 0x0001
    """Events were dropped during recording (backpressure: a slow or full disk).
    The recording is a valid prefix of the truth, not the whole of it; the UI must
    say so. The user's program was never blocked to prevent this -- see writer.py."""


INDEX_ENTRY = struct.Struct("<H Q I")
"""One entry in the INDEX block's payload, 14 bytes -- the location of one block:

    block_type       H    u16  BlockType of the block
    offset           Q    u64  file offset of that block's header (u64: >4 GiB files)
    length           I    u32  bytes of that block (header + payload; u32: bounded)

The INDEX payload is these entries back to back, in file order; there is one per
block in the file, so several EVENTS entries are normal and a reader groups by
type. Redundant with each block's own header on purpose -- it lets a reader plan
every read from the footer alone, without first seeking to each block to learn its
size.
"""
INDEX_ENTRY_SIZE = INDEX_ENTRY.size  # 14


class BlockType(enum.IntEnum):
    """The payload a block carries. Written as u16; see `docs/format-spec.md`.

    The top bit (`OPTIONAL_BLOCK_BIT`) marks a block a reader may skip if it does
    not recognise the type. Required blocks live in `0x0001..0x7FFF`; an unknown
    *required* type means the file uses a feature this reader lacks, and it must
    refuse rather than guess. Values are assigned explicitly and never reused --
    a tag is a permanent part of the on-disk contract.
    """

    META = 0x0001
    """Recording metadata: Python version, platform, ChronoTrace version, config,
    start time, event count. Written first so a reader learns what it is holding
    before touching the body."""

    STRINGS = 0x0002
    """The interning tables: filenames, code-object descriptors, variable names,
    exception type names. Events reference these by the small int ids the recorder
    assigned; this section resolves them."""

    EVENTS = 0x0003
    """A columnar batch of N events: each field stored as its own delta/RLE-friendly
    column. The core of the file. See the format spec for the column order."""

    VALUES = 0x0004
    """The content-addressed value pool: each distinct captured value once,
    msgpack-encoded (day 14), addressed by the `ValueRef` events carry."""

    INDEX = 0x0005
    """The footer index: for every other block, its (type, offset, length). Written
    last; the EOCD points to it. Rebuilt by scanning when absent (crash)."""

    KEYFRAMES = 0x8001
    """Optional (day 15): periodic full-state snapshots that make reaching a past
    instant O(nearest keyframe + bounded deltas). Optional so a v1.0 reader that
    predates keyframes can still open a file that has them, ignoring this block."""

    DELTAS = 0x8002
    """Optional (day 16): the invertible state transitions (bind, frame enter/exit)
    between keyframes -- old and new refs, so a reader can step backward without
    replaying from a keyframe. Optional because deltas are *derivable* from the events
    forward; the block adds the stored old refs (backward stepping) and saves the
    derivation. A reader that does not understand it skips it and reconstructs forward."""


OPTIONAL_BLOCK_BIT = 0x8000
"""A `BlockType` with this bit set is skippable by a reader that does not know it.
The whole forward-compatibility story in one bit: add a feature as a new optional
block type, and every older reader ignores it instead of failing."""


class BlockFlag(enum.IntFlag):
    """Per-block `block_flags` bitfield. Describes how one payload is stored."""

    NONE = 0
    COMPRESSED_ZSTD = 0x0001
    """Payload is a zstd compression frame (day 14): `[u8 codec][u32 raw_length]` then
    the compressed (or, on the incompressible fallback, raw) bytes -- see
    `store/compression.py`. The block CRC covers these stored bytes, so a reader
    verifies the CRC, *then* decompresses. A reader that does not implement this flag
    MUST refuse the block rather than treat the frame as payload -- unlike an EOCD
    flag, a block-storage flag is gating."""
