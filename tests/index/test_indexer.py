"""The driver: one pass, atomic publish, cancellable, and safe to run twice.

These are the properties that decide whether indexing is usable rather than merely
correct. A rebuild that is not idempotent turns a retry into corruption; an uncancellable
build turns a large recording into a hang; a non-atomic publish lets a reader see half an
index and trust it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from chronotrace.index import Cancelled, build_index, schema
from chronotrace.index.db import Batcher, sidecar_path
from chronotrace.recorder import MemorySink, Recorder
from chronotrace.recorder.scope import Scope
from chronotrace.store import ChronoReader, ChronoWriter

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _recording(tmp_path: Path, *, truncated: bool = False) -> Path:
    from chronotrace.cli import intern_tables
    from tests.fixtures import programs

    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(roots=[str(FIXTURES)]))
    with recorder:
        programs.mutates_in_place()
        programs.countdown(3)
    path = tmp_path / "r.chrono"
    with path.open("wb") as handle:
        writer = ChronoWriter(handle)
        writer.add_strings(intern_tables(recorder))
        for captured in recorder.values:
            writer.add_value(captured)
        for event in sink.events:
            writer.add(event)
        writer.close(truncated=truncated)
    return path


def _rows(path: Path) -> list[tuple[Any, ...]]:
    """Read and *close* -- on Windows a held connection blocks the next atomic swap."""
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT name_id, seq, frame_id, value_ref FROM var_writes ORDER BY name_id, seq"
        ).fetchall()
    finally:
        connection.close()


def test_it_indexes_and_lands_beside_the_recording(tmp_path: Path) -> None:
    recording = _recording(tmp_path)
    with ChronoReader.open(recording) as reader:
        result = build_index(recording, reader)
    assert result.path == sidecar_path(recording)
    assert result.path.exists()
    assert result.events == len(ChronoReader.open(recording))
    assert result.rows > 0
    assert not result.partial


def test_rebuilding_is_idempotent(tmp_path: Path) -> None:
    """Running it twice must produce the same rows, not duplicates or an error.

    ADR-0008 requires this: an index is derived state that anything may rebuild at any
    time, so a retry after a failure has to be safe.
    """
    recording = _recording(tmp_path)
    with ChronoReader.open(recording) as reader:
        first = build_index(recording, reader)
    before = _rows(first.path)
    with ChronoReader.open(recording) as reader:
        second = build_index(recording, reader)
    assert _rows(second.path) == before
    assert second.rows == first.rows


def test_a_fresh_index_is_not_stale_and_a_changed_recording_is(tmp_path: Path) -> None:
    recording = _recording(tmp_path)
    with ChronoReader.open(recording) as reader:
        result = build_index(recording, reader)
    db = sqlite3.connect(result.path)
    assert schema.staleness(db, recording) is None
    recording.write_bytes(recording.read_bytes() + b"appended")
    assert schema.staleness(db, recording) is not None


def test_a_truncated_recording_is_indexed_and_flagged_partial(tmp_path: Path) -> None:
    """The crashed recording is the one most worth querying, so it gets an index anyway --
    built from the recovered prefix, and marked so the caller can say so."""
    recording = _recording(tmp_path, truncated=True)
    with ChronoReader.open(recording) as reader:
        result = build_index(recording, reader)
    assert result.partial
    assert result.rows > 0


def test_cancellation_publishes_nothing(tmp_path: Path) -> None:
    """A cancelled build must be indistinguishable from one that never ran.

    Cancelling is checked on the progress cadence, so this drives the check directly
    rather than recording a million events to reach it.
    """
    recording = _recording(tmp_path)
    with ChronoReader.open(recording) as reader, pytest.raises(Cancelled):
        build_index(recording, reader, should_cancel=lambda: True, on_progress=None)
    # No sidecar was published; the temporary file is never read by anyone.
    assert not sidecar_path(recording).exists()


def test_progress_reaches_the_end(tmp_path: Path) -> None:
    """Reported at least once, and the last report is complete -- a progress bar that
    stops at 97% is a bug report."""
    recording = _recording(tmp_path)
    seen: list[float] = []
    with ChronoReader.open(recording) as reader:
        build_index(recording, reader, on_progress=lambda p: seen.append(p.fraction))
    assert seen and seen[-1] == pytest.approx(1.0)


def test_the_recordings_own_intern_tables_land_in_the_index(tmp_path: Path) -> None:
    """Q8: the user types text, the index speaks ids. Without this every other query is
    unusable from a file (issue #6, closed by format 1.6)."""
    recording = _recording(tmp_path)
    with ChronoReader.open(recording) as reader:
        result = build_index(recording, reader)
    db = sqlite3.connect(result.path)
    assert db.execute("SELECT count(*) FROM strings").fetchone()[0] > 0
    assert db.execute("SELECT id FROM strings WHERE text='data'").fetchone() is not None
    path = db.execute(
        "SELECT f.path FROM codes c JOIN files f ON c.file_id = f.file_id LIMIT 1"
    ).fetchone()[0]
    assert path.endswith(".py"), "codes must resolve to a file without the original .pyc"
    assert db.execute("SELECT count(*) FROM exc_types").fetchone()[0] >= 0


def test_the_batcher_never_loses_a_partial_batch() -> None:
    """The failure this guards is silent: up to BATCH_ROWS events simply missing from the
    index, which looks like a query with slightly wrong answers."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t(a INT)")
    with Batcher(db, "INSERT INTO t VALUES (?)", size=4) as batch:
        for i in range(10):  # 2 full batches + a partial one
            batch.add((i,))
    assert db.execute("SELECT count(*) FROM t").fetchone()[0] == 10
    assert batch.rows == 10
