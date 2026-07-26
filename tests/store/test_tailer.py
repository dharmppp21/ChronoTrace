"""The live tailer: every event once, in order; torn tails retried; the writer's death handled.

The cross-process test is the important one -- it pins the Windows file-sharing answer (a reader
can tail a `.chrono` another process holds open for writing) in CI, so a regression fails here
rather than surprising someone on day 49.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from chronotrace.recorder.events import Event, EventKind
from chronotrace.store import ChronoReader, StreamingFileSink
from chronotrace.store.strings import Strings
from chronotrace.store.tailer import ChronoTailer, TailState
from chronotrace.store.writer import ChronoWriter


def _ev(seq: int, kind: EventKind = EventKind.LINE) -> Event:
    return Event(
        seq=seq,
        kind=kind,
        timestamp_ns=seq,
        thread_id=1,
        frame_id=1,
        code_id=0,
        lineno=seq % 10 + 1,
    )


# A writer that streams to a file over time, in its own process -- the cross-process case.
_WRITER = """
import sys, time
from chronotrace.recorder.events import Event, EventKind
from chronotrace.store.writer import StreamingFileSink
from chronotrace.store.strings import Strings
path, n = sys.argv[1], int(sys.argv[2])
sink = StreamingFileSink(path, block_events=4, flush_interval=0.0)
for seq in range(n):
    sink.emit(Event(seq=seq, kind=EventKind.LINE, timestamp_ns=seq, thread_id=1,
                    frame_id=1, code_id=0, lineno=seq % 10 + 1))
    time.sleep(0.01)
sink.close()
sink.finalize([], Strings())
"""


def test_tailer_sees_every_event_once_and_in_order(tmp_path: Path) -> None:
    path = tmp_path / "run.chrono"
    with path.open("wb") as handle:
        writer = ChronoWriter(handle, block_events=4)
        tailer = ChronoTailer(path)
        seen: list[int] = []
        for seq in range(10):
            writer.add(_ev(seq))
            if seq % 3 == 0:
                writer.flush()
                handle.flush()
                seen += [e.seq for e in tailer.poll()]
        writer.flush()
        handle.flush()
        seen += [e.seq for e in tailer.poll()]
        writer.close()
        handle.flush()
        seen += [e.seq for e in tailer.poll()]
    assert seen == list(range(10))  # every event, once, in order
    assert tailer.state is TailState.COMPLETE


def test_an_unflushed_block_waits_then_appears(tmp_path: Path) -> None:
    path = tmp_path / "run.chrono"
    with path.open("wb") as handle:
        writer = ChronoWriter(handle, block_events=8)  # a block needs 8; we add 4
        tailer = ChronoTailer(path)
        for seq in range(4):
            writer.add(_ev(seq))
        handle.flush()
        assert tailer.poll() == []  # buffered, not on disk yet -> nothing, still running
        assert tailer.state is TailState.RUNNING
        writer.flush()  # now the block is complete on disk
        handle.flush()
        assert [e.seq for e in tailer.poll()] == [0, 1, 2, 3]


def test_a_torn_tail_is_retried_not_rejected(tmp_path: Path) -> None:
    path = tmp_path / "run.chrono"
    with path.open("wb") as handle:
        writer = ChronoWriter(handle, block_events=4)
        for seq in range(4):
            writer.add(_ev(seq))
        writer.flush()
        handle.flush()
        # a half-written block: a plausible header, a short payload -- the writer mid-block
        handle.write(b"\x99\x99\x99\x99\x03\x00\x00\x00\x11\x22\x33\x44partial")
        handle.flush()
        tailer = ChronoTailer(path)
        assert [e.seq for e in tailer.poll()] == [0, 1, 2, 3]  # complete blocks decoded
        assert tailer.state is TailState.RUNNING  # the torn tail is a "come back", not an error


def test_a_dead_writer_with_no_footer_is_truncated(tmp_path: Path) -> None:
    path = tmp_path / "run.chrono"
    with path.open("wb") as handle:
        writer = ChronoWriter(handle, block_events=4)
        for seq in range(4):
            writer.add(_ev(seq))
        writer.flush()
        handle.flush()
        handle.write(b"\x99\x99\x99\x99\x03\x00\x00\x00torn")  # never finished, no footer
    tailer = ChronoTailer(path)
    assert [e.seq for e in tailer.poll()] == [0, 1, 2, 3]  # the valid prefix, torn tail retried
    tailer.mark_dead()  # the caller's verdict: the writer is gone
    assert tailer.state is TailState.TRUNCATED
    assert tailer.truncation is not None  # the crash-recovery classifier said why


def test_a_reader_can_tail_a_file_the_writer_holds_open(tmp_path: Path) -> None:
    # The Windows sharing property, in-process: the StreamingFileSink keeps the file open "wb"
    # for its whole life, and the tailer opens+reads it anyway.
    path = tmp_path / "run.chrono"
    sink = StreamingFileSink(path, block_events=4, flush_interval=0.0)
    try:
        for seq in range(8):
            sink.emit(_ev(seq))
        tailer = ChronoTailer(path)  # opens a file the sink still holds open for writing
        assert [e.seq for e in tailer.poll()] == list(range(8))
    finally:
        sink.close()
        sink.finalize([], Strings())


def test_cross_process_tailing(tmp_path: Path) -> None:
    path = tmp_path / "run.chrono"
    writer = subprocess.Popen([sys.executable, "-c", _WRITER, str(path), "20"])  # noqa: S603
    try:
        tailer = ChronoTailer(path)
        seen: list[int] = []
        deadline = time.monotonic() + 20.0
        while tailer.state is TailState.RUNNING and time.monotonic() < deadline:
            seen += [e.seq for e in tailer.poll()]
            time.sleep(0.02)
        seen += [e.seq for e in tailer.poll()]
    finally:
        writer.wait(timeout=20)
    assert seen == list(range(20)), f"tailed a file another process wrote; got {seen}"
    assert tailer.state is TailState.COMPLETE


def test_two_tailers_share_one_recording(tmp_path: Path) -> None:
    path = tmp_path / "run.chrono"
    with path.open("wb") as handle:
        writer = ChronoWriter(handle, block_events=4)
        for seq in range(8):
            writer.add(_ev(seq))
        writer.close()
        handle.flush()
    a, b = ChronoTailer(path), ChronoTailer(path)
    assert [e.seq for e in a.poll()] == list(range(8))
    assert [e.seq for e in b.poll()] == list(range(8))  # independent cursors, same file


def test_a_streamed_recording_finalizes_complete_and_scrubbable(tmp_path: Path) -> None:
    path = tmp_path / "run.chrono"
    sink = StreamingFileSink(path, block_events=4, flush_interval=0.0)
    for seq in range(12):
        sink.emit(_ev(seq))
    sink.close()
    sink.finalize([], Strings())
    with ChronoReader.open(path) as reader:
        assert len(reader) == 12  # events streamed during the run survive the footer
        assert not reader.truncated


def test_tailer_waits_for_a_missing_or_headerless_file(tmp_path: Path) -> None:
    path = tmp_path / "not_yet.chrono"
    tailer = ChronoTailer(path)
    assert tailer.poll() == []  # file does not exist yet -> wait, no crash
    path.write_bytes(b"\x89CHR")  # a few bytes, less than a header
    assert tailer.poll() == []  # header not complete -> still waiting
    assert tailer.state is TailState.RUNNING
