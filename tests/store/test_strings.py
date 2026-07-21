"""The recording carries its own intern tables, so ids outlive the process that made them.

Without this a `.chrono` opened later shows `code#3` and cannot answer "every write to
`total`" at all -- there is no way to turn the text a user types into the id the events
store. Issue #6; the reason the index could not exist before it (ADR-0008 section 7).
"""

from __future__ import annotations

import io

import pytest

from chronotrace.store import ChronoReader, ChronoWriter, CodeInfo, Strings
from chronotrace.store.strings import MAX_ENTRIES, decode_strings, encode_strings


def _written(strings: Strings) -> ChronoReader:
    buf = io.BytesIO()
    writer = ChronoWriter(buf)
    writer.add_strings(strings)
    writer.close()
    return ChronoReader.from_bytes(buf.getvalue())


def test_the_three_id_spaces_round_trip_through_a_real_file() -> None:
    """Names, exception types and code objects, each indexed by the id events carry."""
    strings = Strings(
        names=("total", "i", "bucket"),
        exc_types=("ValueError", "KeyError"),
        codes=(CodeInfo("a.py", "main", 3), CodeInfo("b.py", "Klass.method", 40)),
    )
    restored = _written(strings).strings()
    assert restored == strings
    assert restored.names[2] == "bucket"  # position IS the name_id
    assert restored.codes[1].qualname == "Klass.method"


def test_a_recording_without_strings_reports_empty_not_broken() -> None:
    """Format 1.5 and earlier, or a crash that lost the block. Ids without names are a
    degraded view, not a failure -- the REPL still steps, it just shows numbers."""
    buf = io.BytesIO()
    ChronoWriter(buf).close()
    assert ChronoReader.from_bytes(buf.getvalue()).strings() == Strings()


def test_unicode_and_dunder_names_survive() -> None:
    strings = Strings(names=("café", "__init__", "变量"), codes=(CodeInfo("ünï.py", "f", 1),))
    assert _written(strings).strings() == strings


def test_a_hostile_count_is_refused_before_it_allocates() -> None:
    """Parses untrusted input: a recording arrives in a stranger's bug report."""
    with pytest.raises(ValueError, match="over the"):
        decode_strings((MAX_ENTRIES + 1).to_bytes(4, "little"))


def test_a_length_that_overruns_the_block_is_refused() -> None:
    payload = bytearray(encode_strings(Strings(names=("x",))))
    payload[4:8] = (1 << 15).to_bytes(4, "little")  # claim 32 KiB for a 1-byte string
    with pytest.raises(ValueError, match="overruns"):
        decode_strings(bytes(payload))
