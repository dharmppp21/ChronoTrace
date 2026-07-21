"""Day 26: what indexing costs, and whether the received wisdom about it is true.

Run: `python benchmarks/bench_index.py`

Two questions:

1. **How fast does an index build, and how big is it?** ADR-0008 promised ~14.5 B/event
   and a build measured in seconds; this is the check on real recordings rather than
   synthetic rows.
2. **Is "create indexes after bulk load" actually faster here?** It is standard advice,
   and standard advice is a hypothesis. Three layouts are measured on identical data:
   the clustered `WITHOUT ROWID` table the schema ships, a rowid table with the index
   built afterwards, and a rowid table with it built first.
"""

from __future__ import annotations

import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

from workloads import WORKLOADS  # type: ignore[import-not-found]  # noqa: E402

from chronotrace.cli import intern_tables  # noqa: E402
from chronotrace.index import build_index, last_write_before  # noqa: E402
from chronotrace.recorder import MemorySink, Recorder  # noqa: E402
from chronotrace.recorder.scope import Scope  # noqa: E402
from chronotrace.store import ChronoReader, ChronoWriter  # noqa: E402

SYNTHETIC_ROWS = 2_000_000
DISTINCT_NAMES = 800

_CLUSTERED = (
    "CREATE TABLE var_writes(name_id INT, seq INT, frame_id INT, value_ref INT,"
    " PRIMARY KEY(name_id, seq)) WITHOUT ROWID;"
)
_ROWID = "CREATE TABLE var_writes(name_id INT, seq INT, frame_id INT, value_ref INT);"
_INDEX = "CREATE INDEX ix_v ON var_writes(name_id, seq);"


def _record(workdir: Path) -> Path:
    """A real recording of the realistic workload, written to a real file."""
    fn = WORKLOADS["json_pipeline"]
    fn()
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(include=["*"]))
    with recorder:
        fn()
    path = workdir / "bench.chrono"
    with path.open("wb") as handle:
        writer = ChronoWriter(handle)
        writer.add_strings(intern_tables(recorder))
        for captured in recorder.values:
            writer.add_value(captured)
        for event in sink.events:
            writer.add(event)
        writer.close()
    return path


def _layout(label: str, ddl: str, index_sql: str, rows: list[tuple[int, int, int, int]]) -> None:
    """Bulk-load `rows` under one table layout and report build cost, size and query time."""
    path = Path(tempfile.mkdtemp()) / "x.idx"
    db = sqlite3.connect(path)
    db.executescript(
        "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
        "PRAGMA temp_store=MEMORY; PRAGMA cache_size=-65536;"
    )
    db.executescript(ddl)
    start = time.perf_counter()
    for i in range(0, len(rows), 10_000):
        db.executemany("INSERT OR REPLACE INTO var_writes VALUES (?,?,?,?)", rows[i : i + 10_000])
    load = time.perf_counter() - start
    if index_sql:
        db.executescript(index_sql)
    db.commit()
    total = time.perf_counter() - start

    name = rows[0][0]
    start = time.perf_counter()
    for _ in range(2000):
        db.execute(
            "SELECT seq FROM var_writes WHERE name_id=? AND seq<? ORDER BY seq DESC LIMIT 1",
            (name, len(rows) // 2),
        ).fetchone()
    query = (time.perf_counter() - start) / 2000 * 1e6
    db.close()
    print(
        f"  {label:34s} {total:5.2f}s ({load:4.2f} load + {total - load:4.2f} index)"
        f"  {len(rows) / total:>9,.0f} rows/s  {path.stat().st_size / 1e6:6.1f} MB"
        f"  query {query:5.1f} us"
    )


def main() -> int:
    workdir = Path(tempfile.mkdtemp())
    recording = _record(workdir)
    with ChronoReader.open(recording) as reader:
        events = len(reader)
        start = time.perf_counter()
        result = build_index(recording, reader)
        elapsed = time.perf_counter() - start

    size = result.path.stat().st_size
    print(
        f"json_pipeline: {events:,} events, recording {recording.stat().st_size:,} B\n"
        f"  index built in {elapsed:.2f}s = {events / elapsed:,.0f} events/s\n"
        f"  {result.rows:,} rows, {size:,} B = {size / events:.1f} B/event"
        f" = {size / recording.stat().st_size:.1f}x the recording"
    )

    db = sqlite3.connect(result.path)
    name_id = db.execute("SELECT name_id FROM var_writes LIMIT 1").fetchone()[0]
    start = time.perf_counter()
    for _ in range(2000):
        last_write_before(db, name_id, events // 2)
    print(f"  last-write-before-seq: {(time.perf_counter() - start) / 2000 * 1e6:.1f} us")
    db.close()

    print(f"\nlayouts, {SYNTHETIC_ROWS:,} synthetic rows over {DISTINCT_NAMES} names:")
    rng = random.Random(7)  # noqa: S311 -- reproducibility, not security
    rows = [
        (rng.randrange(DISTINCT_NAMES), seq, rng.randrange(500), seq % 50)
        for seq in range(SYNTHETIC_ROWS)
    ]
    _layout("WITHOUT ROWID (what we ship)", _CLUSTERED, "", rows)
    _layout("rowid, index AFTER load", _ROWID, _INDEX, rows)
    _layout("rowid, index BEFORE load", _ROWID + _INDEX, "", rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
