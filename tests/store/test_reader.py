"""ChronoReader: random access, laziness, and the well-formed edge cases.

Hostile and truncated-at-every-offset files live in test_reader_hostile.py; this
file proves the reader does the right thing on *valid* input.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from chronotrace.recorder.events import Event, EventKind
from chronotrace.store import ChronoReader, TruncatedRecording
from chronotrace.store.writer import ChronoWriter, FileSink


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


def _bytes_of(events: list[Event], *, block_events: int = 65536) -> bytes:
    buf = io.BytesIO()
    writer = ChronoWriter(buf, block_events=block_events)
    for event in events:
        writer.add(event)
    writer.close()
    return buf.getvalue()


@pytest.mark.parametrize("n", [0, 1, 1000])
def test_open_a_real_file_and_round_trip(tmp_path: Path, n: int) -> None:
    path = tmp_path / "rec.chrono"
    events = [_ev(s) for s in range(n)]
    sink = FileSink(path)
    for event in events:
        sink.emit(event)
    sink.close()

    with ChronoReader.open(path) as reader:
        assert len(reader) == n
        assert list(reader.iter_events()) == events


def test_random_access_by_seq() -> None:
    events = [_ev(s) for s in range(500)]
    reader = ChronoReader.from_bytes(_bytes_of(events, block_events=64))  # ~8 blocks
    assert reader[0] == events[0]
    assert reader[250] == events[250]
    assert reader[499] == events[499]
    assert reader[-1] == events[499]  # negative index
    assert reader[10:14] == events[10:14]  # slice


def test_out_of_range_seq_raises_indexerror() -> None:
    reader = ChronoReader.from_bytes(_bytes_of([_ev(0), _ev(1)]))
    with pytest.raises(IndexError):
        reader[2]
    with pytest.raises(IndexError):
        reader[-3]


def test_empty_file_is_truncated() -> None:
    with pytest.raises(TruncatedRecording):
        ChronoReader.from_bytes(b"")


def test_sub_header_file_is_truncated() -> None:
    with pytest.raises(TruncatedRecording):
        ChronoReader.from_bytes(b"\x89CHRONO")  # a few bytes, less than a header


def test_header_only_file_opens_empty() -> None:
    """Header written, nothing else (a crash right after start). Opens, no events."""
    header = _bytes_of([])[:32]  # just the 32-byte header
    reader = ChronoReader.from_bytes(header)
    assert len(reader) == 0
    assert reader.truncated is True  # no footer


def test_clean_empty_recording_is_not_truncated() -> None:
    reader = ChronoReader.from_bytes(_bytes_of([]))
    assert len(reader) == 0
    assert reader.truncated is False


def test_truncated_flag_is_read_from_the_footer() -> None:
    buf = io.BytesIO()
    writer = ChronoWriter(buf)
    writer.add(_ev(0))
    writer.close(truncated=True)
    assert ChronoReader.from_bytes(buf.getvalue()).truncated is True


def test_blocks_are_decoded_lazily_and_cached(monkeypatch: Any) -> None:
    """Open decodes nothing; each new block decodes once; a re-read is an LRU hit."""
    from chronotrace.store.columnar import decode_events as real

    calls = 0

    def counting(payload: bytes) -> list[Event]:
        nonlocal calls
        calls += 1
        return real(payload)

    monkeypatch.setattr("chronotrace.store.reader.decode_events", counting)

    reader = ChronoReader.from_bytes(_bytes_of([_ev(s) for s in range(6)], block_events=2))
    assert calls == 0, "open() must not decode any block"
    reader[0]
    assert calls == 1, "first access decodes its block"
    reader[1]
    assert calls == 1, "same block: an LRU hit, no re-decode"
    reader[4]  # a different block
    assert calls == 2


def test_open_and_from_bytes_agree(tmp_path: Path) -> None:
    events = [_ev(s) for s in range(100)]
    path = tmp_path / "rec.chrono"
    sink = FileSink(path)
    for event in events:
        sink.emit(event)
    sink.close()

    with ChronoReader.open(path) as via_file:
        via_bytes = ChronoReader.from_bytes(path.read_bytes())
        assert list(via_file.iter_events()) == list(via_bytes.iter_events())
