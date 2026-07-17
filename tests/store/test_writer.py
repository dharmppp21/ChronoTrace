"""The writer end to end: byte-exact to the spec, lossless round-trip, and correct
under the failures the format was designed to survive (crash, drop, binary mode)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from chronotrace.recorder.events import Event, EventKind
from chronotrace.store import ChronoReader, CorruptRecording, UnsupportedVersion
from chronotrace.store.writer import ChronoWriter, FileSink


def _read(data: bytes) -> tuple[list[Event], bool]:
    """Read a whole in-memory .chrono buffer back, via the real reader."""
    reader = ChronoReader.from_bytes(data)
    return list(reader.iter_events()), reader.truncated


# The minimal file from docs/format-spec.md §12, produced by the writer with zero
# events. This locks the format: any layout change fails here, loudly.
_MINIMAL_HEX = (
    "894348524f4e4f0d0a1a0a0100000000000000000000002000000000000000000100000001000000"
    "ad6cba3f800e00000005000000949befa6010020000000000000000d0000002d000000000000001a"
    "00000000000000949befa6000000004348524f4e454e44"
)


def _ev(seq: int) -> Event:
    return Event(
        seq=seq,
        kind=EventKind.LINE,
        timestamp_ns=1000 + seq,
        thread_id=1,
        frame_id=1,
        code_id=1,
        lineno=10 + seq % 7,
    )


def test_minimal_file_is_byte_exact_to_the_spec() -> None:
    buf = io.BytesIO()
    ChronoWriter(buf).close()
    data = buf.getvalue()
    assert data == bytes.fromhex(_MINIMAL_HEX)
    assert len(data) == 103


@pytest.mark.parametrize("n", [0, 1, 2, 1000])
def test_round_trip_through_a_real_file(tmp_path: Path, n: int) -> None:
    path = tmp_path / "rec.chrono"
    events = [_ev(s) for s in range(n)]
    sink = FileSink(path)
    for event in events:
        sink.emit(event)
    sink.close()

    read_back, truncated = _read(path.read_bytes())
    assert read_back == events
    assert truncated is False


def test_filesink_bytes_match_the_pure_writer_no_text_mode_mangling(tmp_path: Path) -> None:
    """`wb`, never `w`: Windows text mode would turn every 0x0A into 0x0D 0x0A."""
    events = [_ev(s) for s in range(50)]
    path = tmp_path / "rec.chrono"
    sink = FileSink(path)
    for event in events:
        sink.emit(event)
    sink.close()

    buf = io.BytesIO()
    writer = ChronoWriter(buf)
    for event in events:
        writer.add(event)
    writer.close()

    assert path.read_bytes() == buf.getvalue()
    assert b"\r\n" not in path.read_bytes() or b"\n" in buf.getvalue()  # sanity: no injected CRLF


def test_many_small_blocks_round_trip() -> None:
    events = [_ev(s) for s in range(5)]
    buf = io.BytesIO()
    writer = ChronoWriter(buf, block_events=2)  # 3 EVENTS blocks for 5 events
    for event in events:
        writer.add(event)
    writer.close()

    read_back, truncated = _read(buf.getvalue())
    assert read_back == events
    assert truncated is False


def test_truncated_flag_round_trips() -> None:
    buf = io.BytesIO()
    writer = ChronoWriter(buf)
    writer.add(_ev(0))
    writer.close(truncated=True)

    _events, truncated = _read(buf.getvalue())
    assert truncated is True


def test_crash_between_blocks_recovers_the_prefix() -> None:
    """No footer (process killed mid-write): the readable prefix survives."""
    buf = io.BytesIO()
    writer = ChronoWriter(buf, block_events=2)
    for s in range(6):  # three EVENTS blocks
        writer.add(_ev(s))
    writer.close()
    full = buf.getvalue()

    # Simulate a kill after the second EVENTS block: drop the footer and last block.
    # Two full events blocks + header + META remain; chop somewhere in the third.
    crashed = full[: len(full) // 2]
    events, truncated = _read(crashed)
    assert truncated is True
    assert events == [_ev(s) for s in range(len(events))]  # a clean prefix, no partial event
    assert events, "at least the first block should be recoverable"


def test_filesink_drops_on_write_failure_without_raising(tmp_path: Path) -> None:
    """A disk failure must never reach the traced program; it truncates instead."""

    class _FailAfter:
        def __init__(self, real: ChronoWriter, n: int) -> None:
            self._real, self._n, self._calls = real, n, 0

        def add(self, event: Event) -> None:
            self._calls += 1
            if self._calls > self._n:
                raise OSError("disk full")
            self._real.add(event)

        def close(self, *, truncated: bool = False) -> None:
            self._real.close(truncated=truncated)

    path = tmp_path / "rec.chrono"
    sink = FileSink(path, block_events=2)
    sink._writer = _FailAfter(sink._writer, n=2)  # type: ignore[assignment]

    for s in range(5):
        sink.emit(_ev(s))  # events 3-5 fail; emit must not raise
    sink.close()

    assert sink.truncated is True
    events, truncated = _read(path.read_bytes())
    assert truncated is True
    assert events == [_ev(0), _ev(1)]  # the one block written before the failure


def test_a_non_chrono_file_is_rejected() -> None:
    with pytest.raises(CorruptRecording, match=r"not a \.chrono"):
        _read(b"this is not a chrono file at all")  # 32 bytes: long enough to reach the magic check


def test_a_newer_major_version_is_refused() -> None:
    buf = io.BytesIO()
    ChronoWriter(buf).close()
    data = bytearray(buf.getvalue())
    data[11] = 99  # bump version_major in the header (offset 11)
    with pytest.raises(UnsupportedVersion, match="upgrade"):
        _read(bytes(data))
