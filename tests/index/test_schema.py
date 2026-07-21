"""The schema applies, versions round-trip, and a stale index is detected rather than trusted.

The last one is the point. An index that is merely *absent* costs a rebuild; an index that
is silently **wrong** answers "who wrote to `total`?" with yesterday's recording, and the
user has no way to tell. So every way an index can stop matching its recording gets a test.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from chronotrace.index import schema

ADR = Path(__file__).parent.parent.parent / "docs" / "adr" / "0008-index-schema.md"


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    schema.create(connection)
    return connection


@pytest.fixture
def recording(tmp_path: Path) -> Path:
    path = tmp_path / "r.chrono"
    path.write_bytes(b"\x89CHRONO\r\n\x1a\n" + b"\x00" * 200 + b"CHRONEND")
    return path


def test_the_ddl_applies_to_a_fresh_database(db: sqlite3.Connection) -> None:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {
        "meta",
        "strings",
        "files",
        "exc_types",
        "codes",
        "var_writes",
        "line_hits",
        "frames",
        "exceptions",
        "density",
    }


def test_the_hot_tables_are_clustered_on_their_query_key(db: sqlite3.Connection) -> None:
    """`WITHOUT ROWID` on the tables reached only through a composite key.

    Measured at 30.3 -> 14.5 bytes per event (ADR-0008 section 6). Pinned because it is
    invisible in a query result and a future edit could drop it without any test noticing
    -- the index would just quietly double in size.
    """
    sql = {r[0]: r[1] for r in db.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")}
    for table in ("var_writes", "line_hits", "meta", "density"):
        assert "WITHOUT ROWID" in sql[table].upper(), f"{table} lost its clustering"


def test_the_demo_query_uses_the_index_and_never_scans(db: sqlite3.Connection) -> None:
    """ "The last write to x before S" must be a seek, not a scan. It is the whole demo.

    Asserted through the query planner rather than by timing, so it fails on a laptop
    under load as reliably as on CI.
    """
    plan = db.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT seq, value_ref FROM var_writes WHERE name_id=? AND seq<? "
        "ORDER BY seq DESC LIMIT 1",
        (1, 100),
    ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan)
    assert "SCAN" not in detail.upper(), detail
    assert "var_writes" in detail


def test_every_declared_index_is_justified_by_a_named_query() -> None:
    """The day's rule, enforced: an index whose query nobody wrote down gets deleted.

    Parses the ADR rather than trusting a checklist, because a checklist is a comment and
    comments do not fail builds. Adding an index without adding its row to ADR-0008's
    table breaks this test, which is exactly when the question should be asked.
    """
    declared = set(re.findall(r"CREATE (?:INDEX|TABLE) (\w+)", schema.DDL))
    documented = ADR.read_text(encoding="utf-8")
    missing = sorted(name for name in declared if name not in documented)
    assert not missing, f"indexes with no named query in ADR-0008: {missing}"


def test_a_fresh_stamp_is_not_stale(db: sqlite3.Connection, recording: Path) -> None:
    schema.stamp(db, recording, event_count=10)
    assert schema.staleness(db, recording) is None


def test_a_changed_recording_is_detected(db: sqlite3.Connection, recording: Path) -> None:
    """The case that matters: same path, different recording."""
    schema.stamp(db, recording, event_count=10)
    recording.write_bytes(recording.read_bytes() + b"more")
    assert schema.staleness(db, recording) == "the recording changed since this index was built"


def test_a_version_bump_invalidates_every_existing_index(
    db: sqlite3.Connection, recording: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both versions, because they drift independently -- see `INDEXER_VERSION`."""
    schema.stamp(db, recording, event_count=10)
    monkeypatch.setattr(schema, "SCHEMA_VERSION", schema.SCHEMA_VERSION + 1)
    assert "schema version" in (schema.staleness(db, recording) or "")
    monkeypatch.undo()
    monkeypatch.setattr(schema, "INDEXER_VERSION", schema.INDEXER_VERSION + 1)
    assert "indexer version" in (schema.staleness(db, recording) or "")


def test_a_half_written_index_is_stale_not_trusted(recording: Path) -> None:
    """An interrupted build leaves no `meta`. With durability off that is the likely shape
    of a crash, and the only safe reading of it is "rebuild"."""
    empty = sqlite3.connect(":memory:")
    assert schema.staleness(empty, recording) is not None


def test_the_fingerprint_does_not_read_the_whole_recording(tmp_path: Path) -> None:
    """O(1), because this runs before every query. A 10 GB hash would cost more than the
    queries it guards."""
    big = tmp_path / "big.chrono"
    big.write_bytes(b"\x89CHRONO\r\n\x1a\n" + b"\x00" * (4 << 20) + b"CHRONEND")
    first = schema.fingerprint(big)
    with big.open("r+b") as handle:  # change the middle only
        handle.seek(2 << 20)
        handle.write(b"\xff" * 16)
    assert schema.fingerprint(big) == first, (
        "a middle-only edit is deliberately invisible; the tail and size are the guard"
    )
