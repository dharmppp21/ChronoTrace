"""Pins the .chrono format's byte layout so an accidental edit fails CI loudly.

Once a recording exists in the wild the format is a compatibility contract, and
the cheapest place to catch an unintended change is here -- a golden test on the
exact bytes, not a promise in a docstring.
"""

from __future__ import annotations

from chronotrace.store import constants as c

# Common file signatures a .chrono must not be mistaken for.
_KNOWN_SIGNATURES = (
    b"\x89PNG",  # PNG (shares the 0x89 trick; must differ after it)
    b"%PDF",
    b"PK\x03\x04",  # zip / docx / jar
    b"\x1f\x8b",  # gzip
    b"\x7fELF",
    b"BZh",  # bzip2
    b"\xfd7zXZ\x00",  # xz
    b"{",  # json object
    b"[",  # json array
)


def test_header_byte_layout_is_pinned() -> None:
    """A known header must serialise to these exact 32 bytes. See format-spec §12."""
    golden = bytes.fromhex("894348524f4e4f0d0a1a0a010004000000000000000000200000000000000000")
    packed = c.HEADER.pack(
        c.MAGIC, c.FORMAT_VERSION_MAJOR, c.FORMAT_VERSION_MINOR, 0, c.HEADER_SIZE
    )
    assert packed == golden
    assert len(packed) == c.HEADER_SIZE == 32


def test_header_round_trips() -> None:
    magic, major, minor, flags, header_size = c.HEADER.unpack(
        c.HEADER.pack(c.MAGIC, 1, 0, 0, c.HEADER_SIZE)
    )
    assert magic == c.MAGIC
    assert (major, minor) == (1, 0)
    assert flags == 0
    assert header_size == c.HEADER_SIZE


def test_magic_is_not_valid_utf8() -> None:
    """The 0x89 lead byte means the file cannot be mistaken for text."""
    try:
        c.MAGIC.decode("utf-8")
    except UnicodeDecodeError:
        return
    raise AssertionError("MAGIC decoded as UTF-8; it must not look like text")


def test_magic_is_not_a_known_file_signature() -> None:
    for sig in _KNOWN_SIGNATURES:
        assert not c.MAGIC.startswith(sig), f"MAGIC collides with a known signature: {sig!r}"


def test_eocd_magic_is_distinct_from_the_file_magic() -> None:
    assert c.EOCD_MAGIC != c.MAGIC
    assert not c.MAGIC.startswith(c.EOCD_MAGIC)
    assert not c.EOCD_MAGIC.startswith(c.MAGIC[:8])


def test_block_type_tags_do_not_collide() -> None:
    values = [t.value for t in c.BlockType]
    assert len(values) == len(set(values)), "duplicate block-type tag"


def test_optional_bit_convention_holds() -> None:
    """Required tags in 0x0001..0x7FFF; optional tags set OPTIONAL_BLOCK_BIT."""
    required = {
        c.BlockType.META,
        c.BlockType.STRINGS,
        c.BlockType.EVENTS,
        c.BlockType.VALUES,
        c.BlockType.INDEX,
    }
    for tag in required:
        assert not tag & c.OPTIONAL_BLOCK_BIT, f"{tag.name} must be a required tag"
    assert c.BlockType.KEYFRAMES & c.OPTIONAL_BLOCK_BIT, "KEYFRAMES must be optional"


def test_struct_sizes_are_pinned() -> None:
    """Literal sizes, so a layout change fails here (the _SIZE consts are derived)."""
    assert c.HEADER.size == 32
    assert c.BLOCK_HEADER.size == 12
    assert c.EOCD.size == 32  # grew from 28 on day 12: gained a u32 flags field
    assert c.INDEX_ENTRY.size == 14


def test_all_structs_are_little_endian() -> None:
    """Endianness is fixed by the format, never the host: every struct starts '<'."""
    for s in (c.HEADER, c.BLOCK_HEADER, c.EOCD, c.INDEX_ENTRY):
        assert s.format.startswith("<"), f"{s.format} is not little-endian"
