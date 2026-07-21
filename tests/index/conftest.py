"""Record a program, write a real `.chrono`, index it -- the setup every index test needs.

Extracted rather than repeated: two test files already had a copy before today added four
more, and the whole point of these tests is that they run against the *real* pipeline
(recorder -> writer -> reader -> indexer), so the setup is genuinely shared rather than
incidentally similar.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from chronotrace.cli import intern_tables
from chronotrace.index import build_index
from chronotrace.recorder import Event, MemorySink, Recorder
from chronotrace.recorder.scope import Scope
from chronotrace.store import ChronoReader, ChronoWriter

FIXTURES = Path(__file__).parent.parent / "fixtures"
EXAMPLES = Path(__file__).parent.parent.parent / "examples"


@dataclass(frozen=True, slots=True)
class Indexed:
    """A recorded, written and indexed program, with the lookups a query needs."""

    db: sqlite3.Connection
    events: list[Event]
    names: dict[str, int]
    files: dict[str, int]
    codes: dict[str, int]
    exc_types: dict[str, int]
    recording: Path

    def file_id(self, suffix: str) -> int:
        """The `file_id` whose path ends with `suffix` -- tests should not hardcode paths."""
        for path, ident in self.files.items():
            if path.endswith(suffix):
                return ident
        raise KeyError(f"no indexed file ends with {suffix!r}: {sorted(self.files)}")


def index_program(tmp_path: Path, fn: Callable[[], object], roots: Path, name: str) -> Indexed:
    """Record `fn`, persist it, index it, and hand back everything a test needs.

    Scoped to `roots` so the stream is the program's own rather than pytest's.
    """
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(roots=[str(roots)]))
    with recorder:
        fn()
    recording = tmp_path / f"{name}.chrono"
    with recording.open("wb") as handle:
        writer = ChronoWriter(handle)
        writer.add_strings(intern_tables(recorder))
        for captured in recorder.values:
            writer.add_value(captured)
        for event in sink.events:
            writer.add(event)
        writer.close()
    with ChronoReader.open(recording) as reader:
        result = build_index(recording, reader)
    db = sqlite3.connect(result.path)
    return Indexed(
        db=db,
        events=sink.events,
        names={text: ident for ident, text in db.execute("SELECT id, text FROM strings")},
        files={path: ident for ident, path in db.execute("SELECT file_id, path FROM files")},
        codes={q: ident for ident, q in db.execute("SELECT code_id, qualname FROM codes")},
        exc_types={t: i for i, t in db.execute("SELECT id, text FROM exc_types")},
        recording=recording,
    )


def index_example(tmp_path: Path, module: str, func: str = "main") -> Indexed:
    """Index one of `examples/`, which is where the handwritten golden trees come from."""
    sys.path.insert(0, str(EXAMPLES))
    try:
        imported: Any = __import__(module)
        entry = getattr(imported, func)
        return index_program(tmp_path, entry, EXAMPLES, f"{module}_{func}")
    finally:
        sys.path.remove(str(EXAMPLES))


@pytest.fixture
def simple(tmp_path: Path) -> Indexed:
    """`examples/simple.py` -- the 53-event stream a human has already verified by hand."""
    return index_example(tmp_path, "simple")
