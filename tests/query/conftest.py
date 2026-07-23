"""Two ways to get a `QueryContext`: over a real recording, and hand-built around fakes.

The real one (`simple_ctx`) records `examples/simple.py`, writes a `.chrono`, and lets
`QueryContext.open` build the index -- the whole pipeline, so a golden result is a fact
about the product rather than about a mock. The fake one (`fake_ctx`) wires a query to an
in-memory SQLite and a stub reader, which is the point of dependency injection: if a query
can only run against a real recording, the context was never really injected.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from chronotrace.cli import intern_tables
from chronotrace.index import schema
from chronotrace.query import QueryContext
from chronotrace.recorder import MemorySink, Recorder
from chronotrace.recorder.scope import Scope
from chronotrace.store import ChronoReader, ChronoWriter

EXAMPLES = Path(__file__).parent.parent.parent / "examples"


def record_example(tmp_path: Path, module: str, func: str = "main") -> Path:
    """Record one of `examples/` to a real `.chrono` and return its path (no index yet)."""
    sys.path.insert(0, str(EXAMPLES))
    try:
        entry = getattr(__import__(module), func)
    finally:
        sys.path.remove(str(EXAMPLES))
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(roots=[str(EXAMPLES)]))
    with recorder:
        entry()
    recording = tmp_path / f"{module}_{func}.chrono"
    with recording.open("wb") as handle:
        writer = ChronoWriter(handle)
        writer.add_strings(intern_tables(recorder))
        for captured in recorder.values:
            writer.add_value(captured)
        for event in sink.events:
            writer.add(event)
        writer.close()
    return recording


@pytest.fixture
def simple_ctx(tmp_path: Path) -> Iterator[QueryContext]:
    """A context over `examples/simple.py`, index built on open. The golden-result fixture."""
    recording = record_example(tmp_path, "simple")
    with QueryContext.open(recording) as ctx:
        yield ctx


class _FakeReader:
    """The narrowest thing a query's enrichment needs of a reader: a value and a flag.

    `value(ref)` returns the ref itself, so a preview is a stable `repr(ref)` with no pool;
    `truncated` drives `QueryContext.partial`. That a real query runs against this at all is
    the proof the reader is injected, not reached for.
    """

    def __init__(self, *, truncated: bool = False) -> None:
        self.truncated = truncated

    def value(self, ref: int) -> int:
        return ref


def synthetic_db(path: Path | None = None) -> sqlite3.Connection:
    """An empty index with the real schema, in memory or at `path` (for the big fixtures)."""
    connection = sqlite3.connect(":memory:" if path is None else path)
    schema.create(connection)
    return connection


def fake_ctx(db: sqlite3.Connection, *, truncated: bool = False) -> QueryContext:
    """A `QueryContext` around a synthetic index and a stub reader -- no recording involved."""
    return QueryContext(cast(ChronoReader, _FakeReader(truncated=truncated)), db)
