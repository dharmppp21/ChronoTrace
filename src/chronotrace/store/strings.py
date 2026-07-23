"""The recording's intern tables, persisted so ids survive the process that made them.

Events carry `name_id`, `code_id` and `exc_type_id` -- small integers that mean nothing
without the tables that issued them, and those tables live in the recorder, which is gone
by the time anyone queries. Until this block existed, a `.chrono` opened later could show
you `code#3` and could not answer "every write to `total`" at all, because it had no way
to turn the text a user types into the id the events store (issue #7's sibling, #6).

Why this had to exist before the index, not after
-------------------------------------------------
The alternative was to build the index at recording close, while the recorder is still
alive, and take the tables straight from it. That was rejected in ADR-0008 §7 for a
structural reason: an index must be rebuildable **from the recording alone**. Take the
strings from a live recorder and a deleted index can never be rebuilt, and a
crash-truncated recording -- the one most worth querying -- has neither strings nor index.
So the strings belong in the file.

What is stored, and what deliberately is not
--------------------------------------------
Names and exception type names are stored as text. Code objects are stored as
`(filename, qualname, first_lineno)` -- **never** anything that would require the original
`.pyc` to interpret. A recording arriving in a bug report must be readable on a machine
that has never seen the program.

Three separate id spaces, not one pool. The recorder issues `name_id`, `code_id` and
`exc_type_id` independently, and merging them here would renumber every event's fields.
The point of persisting the tables is that an id means the same thing on both sides.

**Parses untrusted input**: every count and every length is bounded before it is used to
allocate, and every slice is checked against the payload it came from.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Final

_COUNT = struct.Struct("<I")
_LINENO = struct.Struct("<I")

MAX_ENTRIES: Final = 1 << 20
"""Cap on any one table's entry count. A million distinct variable names is already far
past any real program; a hostile file claiming more is refused before it allocates."""

MAX_TEXT_BYTES: Final = 1 << 16
"""Cap on one string. Qualnames and filenames are short; 64 KiB is generous and finite."""


@dataclass(frozen=True, slots=True)
class CodeInfo:
    """What a `code_id` resolves to, without needing the code object or its `.pyc`."""

    filename: str
    qualname: str
    first_lineno: int


@dataclass(frozen=True, slots=True)
class Strings:
    """The three intern tables, indexed by the id the events carry, plus source hashes.

    `names[name_id]`, `exc_types[exc_type_id]`, `codes[code_id]` -- dense from 0, which is
    what the recorder's `InternTable` guarantees and what makes a list the right shape.

    `source_hashes` (format 1.7) is `(filename, hex_sha256)` for each recorded source file
    that existed and was readable when the recording was written. It is *not* dense and not
    keyed by any id -- it is a small side table a provenance query consults to refuse
    analysing a source file that has changed since the recording. A file that could not be
    read at record time (deleted, `<string>` from `exec`, unreadable) simply has no entry,
    which the query reads as "cannot verify" rather than "verified".
    """

    names: tuple[str, ...] = ()
    exc_types: tuple[str, ...] = ()
    codes: tuple[CodeInfo, ...] = field(default_factory=tuple)
    source_hashes: tuple[tuple[str, str], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.names or self.exc_types or self.codes)

    def hash_of(self, filename: str) -> str | None:
        """The recorded SHA-256 of `filename`, or None if it was not hashed. O(files)."""
        return next((h for f, h in self.source_hashes if f == filename), None)


def encode_strings(strings: Strings) -> bytes:
    """Serialise the intern tables as a STRINGS block payload (spec §6.2).

    The source-hash table (format 1.7) is written *last*, so an older reader that decodes
    only names/exc_types/codes stops before it and is unaffected -- a trailing addition,
    the same forward-compatibility trick the EVENTS `ncols` uses.
    """
    out = bytearray()
    for table in (strings.names, strings.exc_types):
        out += _COUNT.pack(len(table))
        for text in table:
            out += _text(text)
    out += _COUNT.pack(len(strings.codes))
    for code in strings.codes:
        out += _text(code.filename) + _text(code.qualname) + _LINENO.pack(code.first_lineno)
    out += _COUNT.pack(len(strings.source_hashes))
    for filename, digest in strings.source_hashes:
        out += _text(filename) + _text(digest)
    return bytes(out)


def decode_strings(payload: bytes) -> Strings:
    """Inverse of `encode_strings`. Parses untrusted input.

    Raises:
        ValueError: a count over `MAX_ENTRIES`, a string over `MAX_TEXT_BYTES`, or any
            length that overruns the payload.
        struct.error: the payload is shorter than a header it declares.
    """
    names, pos = _read_table(payload, 0)
    exc_types, pos = _read_table(payload, pos)
    count, pos = _read_count(payload, pos)
    codes = []
    for _ in range(count):
        filename, pos = _read_text(payload, pos)
        qualname, pos = _read_text(payload, pos)
        (lineno,), pos = _LINENO.unpack_from(payload, pos), pos + _LINENO.size
        codes.append(CodeInfo(filename, qualname, lineno))
    source_hashes = _read_source_hashes(payload, pos)
    return Strings(
        names=tuple(names),
        exc_types=tuple(exc_types),
        codes=tuple(codes),
        source_hashes=source_hashes,
    )


def _read_source_hashes(payload: bytes, pos: int) -> tuple[tuple[str, str], ...]:
    """The trailing source-hash table (format 1.7), or () for a pre-1.7 block.

    A block written before 1.7 ends exactly after `codes`, so `pos == len(payload)` and
    there is nothing to read -- absence is the signal, no version number needed here.
    """
    if pos >= len(payload):
        return ()
    count, pos = _read_count(payload, pos)
    hashes = []
    for _ in range(count):
        filename, pos = _read_text(payload, pos)
        digest, pos = _read_text(payload, pos)
        hashes.append((filename, digest))
    return tuple(hashes)


def _text(value: str) -> bytes:
    encoded = value.encode("utf-8", "replace")[:MAX_TEXT_BYTES]
    return _COUNT.pack(len(encoded)) + encoded


def _read_count(payload: bytes, pos: int) -> tuple[int, int]:
    (count,) = _COUNT.unpack_from(payload, pos)
    if not 0 <= count <= MAX_ENTRIES:
        raise ValueError(f"strings table claims {count} entries, over the {MAX_ENTRIES} cap")
    return count, pos + _COUNT.size


def _read_text(payload: bytes, pos: int) -> tuple[str, int]:
    (length,) = _COUNT.unpack_from(payload, pos)
    pos += _COUNT.size
    if not 0 <= length <= MAX_TEXT_BYTES:
        raise ValueError(f"string claims {length} bytes, over the {MAX_TEXT_BYTES} cap")
    if pos + length > len(payload):
        raise ValueError("string overruns the block")
    # `replace` rather than `strict`: a corrupt byte should cost one glyph, not the whole
    # recording's ability to name anything.
    return payload[pos : pos + length].decode("utf-8", "replace"), pos + length


def _read_table(payload: bytes, pos: int) -> tuple[list[str], int]:
    count, pos = _read_count(payload, pos)
    out = []
    for _ in range(count):
        text, pos = _read_text(payload, pos)
        out.append(text)
    return out, pos
