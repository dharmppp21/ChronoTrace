"""The flagship query, against a real recording: who last wrote to `x` before `seq`?

Golden `seq` lists rather than counts. A test that asserts "some writes were found" passes
just as happily when the index is off by one frame or missing its last batch, and those
are exactly the failures that make a debugger lie about causality.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from chronotrace.index import build_index, last_write_before, writes_to
from chronotrace.index.var_writes import DELETED
from chronotrace.recorder import MemorySink, Recorder
from chronotrace.recorder.events import EventKind
from chronotrace.recorder.scope import Scope
from chronotrace.store import ChronoReader, ChronoWriter

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _record(
    fn: Any, tmp_path: Path, name: str = "r"
) -> tuple[Path, sqlite3.Connection, dict[str, int]]:
    """Record `fn`, write a real `.chrono`, index it, and return the pieces a query needs."""
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(roots=[str(FIXTURES)]))
    with recorder:
        fn()
    recording = tmp_path / f"{name}.chrono"
    with recording.open("wb") as handle:
        writer = ChronoWriter(handle)
        writer.add_strings(_strings(recorder))
        for captured in recorder.values:
            writer.add_value(captured)
        for event in sink.events:
            writer.add(event)
        writer.close()
    with ChronoReader.open(recording) as reader:
        result = build_index(recording, reader)
    db = sqlite3.connect(result.path)
    return recording, db, {t: i for i, t in db.execute("SELECT id, text FROM strings")}


def _strings(recorder: Recorder) -> Any:
    from chronotrace.cli import intern_tables

    return intern_tables(recorder)


@pytest.fixture
def indexed(tmp_path: Path) -> tuple[sqlite3.Connection, dict[str, int], list[Any]]:
    """`tests/fixtures/programs.py::mutates_in_place` — three writes to one name."""
    from tests.fixtures import programs

    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(roots=[str(FIXTURES)]))
    with recorder:
        programs.mutates_in_place()
    recording = tmp_path / "m.chrono"
    with recording.open("wb") as handle:
        writer = ChronoWriter(handle)
        writer.add_strings(_strings(recorder))
        for captured in recorder.values:
            writer.add_value(captured)
        for event in sink.events:
            writer.add(event)
        writer.close()
    with ChronoReader.open(recording) as reader:
        result = build_index(recording, reader)
    db = sqlite3.connect(result.path)
    ids = {text: ident for ident, text in db.execute("SELECT id, text FROM strings")}
    return db, ids, sink.events


def test_every_write_to_a_name_is_indexed_with_its_exact_seqs(
    indexed: tuple[sqlite3.Connection, dict[str, int], list[Any]],
) -> None:
    """The golden list: the index must agree with the event stream event for event."""
    db, ids, events = indexed
    name_id = ids["data"]
    expected = [e.seq for e in events if e.kind is EventKind.VAR_WRITE and e.name_id == name_id]
    assert expected, "the fixture must write `data` at least once"
    assert [seq for seq, _frame, _ref in writes_to(db, name_id)] == expected


def test_the_last_write_before_every_boundary(
    indexed: tuple[sqlite3.Connection, dict[str, int], list[Any]],
) -> None:
    """Checked at every write's own seq, one past it, and one before -- the off-by-one
    boundaries. "Before" is strict: the write *at* `seq` is the one the user is looking at."""
    db, ids, _events = indexed
    name_id = ids["data"]
    seqs = [seq for seq, _f, _r in writes_to(db, name_id)]
    for i, seq in enumerate(seqs):
        assert last_write_before(db, name_id, seq) == (
            None if i == 0 else (seqs[i - 1], *_rest(db, name_id, seqs[i - 1]))
        )
        after = last_write_before(db, name_id, seq + 1)
        assert after is not None and after[0] == seq
    assert last_write_before(db, name_id, seqs[0]) is None  # nothing before the first


def _rest(db: sqlite3.Connection, name_id: int, seq: int) -> tuple[int, int]:
    row = db.execute(
        "SELECT frame_id, value_ref FROM var_writes WHERE name_id=? AND seq=?", (name_id, seq)
    ).fetchone()
    return (row[0], row[1])


def test_the_query_planner_actually_uses_the_index(
    indexed: tuple[sqlite3.Connection, dict[str, int], list[Any]],
) -> None:
    """A test that asserts an index is *used* is the difference between hoping and knowing.

    Without this, the query stays correct while silently degrading to a full scan the day
    a column changes -- and on a small fixture nobody would notice until a real recording.
    """
    db, ids, _ = indexed
    plan = " ".join(
        str(row[-1])
        for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT seq, frame_id, value_ref FROM var_writes "
            "WHERE name_id=? AND seq<? ORDER BY seq DESC LIMIT 1",
            (ids["data"], 100),
        )
    )
    assert "SCAN" not in plan.upper(), plan
    assert "SEARCH" in plan.upper() and "var_writes" in plan


def test_recursion_keeps_each_invocations_writes_apart(tmp_path: Path) -> None:
    """The bug the `frame_id` column exists to prevent.

    `countdown(3)` binds `n` in four live frames. Keyed on the name alone, "the last write
    to `n`" would answer with some *other* call's write -- confidently and wrongly.
    """
    from tests.fixtures import programs

    _rec, db, ids = _record(lambda: programs.countdown(3), tmp_path, "rec")
    name_id = ids["n"]
    rows = writes_to(db, name_id)
    frames = {frame for _seq, frame, _ref in rows}
    assert len(frames) >= 4, f"expected one frame per recursive call, got {frames}"
    for frame in frames:
        scoped = writes_to(db, name_id, frame_id=frame)
        assert scoped, "each invocation must have its own writes"
        assert {f for _s, f, _r in scoped} == {frame}
        last = last_write_before(db, name_id, max(s for s, _f, _r in rows) + 1, frame_id=frame)
        assert last is not None and last[1] == frame


def test_a_deleted_binding_is_recorded_as_a_deletion(tmp_path: Path) -> None:
    """`del x` (format 1.5) is a row, not an absence: "when did x stop existing?" is a
    real question, and a deletion is often the answer to "who last wrote to it?"."""
    from tests.fixtures import programs

    _rec, db, ids = _record(programs.deletes_a_local, tmp_path, "del")
    rows = writes_to(db, ids["doomed"])
    assert rows, "the fixture must bind `doomed`"
    assert rows[-1][2] == DELETED, f"the last event for `doomed` should be its deletion: {rows}"
