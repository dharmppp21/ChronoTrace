"""The index schema, as DDL, in one place -- plus the rule that decides when to rebuild.

The DDL string below is the **single source of truth**. Not a migration chain, not an ORM
model: an index is derived state, so there is never anything to migrate. A schema change
bumps `SCHEMA_VERSION` and the next open throws the old file away and rebuilds it. That is
the whole benefit of derived state, and paying for migrations anyway would be paying twice.

Every index is justified by a named query
-----------------------------------------
An index with no query behind it is speculative generality with a storage bill, and this
one is measured: **14.5 bytes per event, three times the size of the recording it
indexes** (ADR-0008). So each `PRIMARY KEY` and `CREATE INDEX` below carries the query it
serves in a comment, and one candidate index was deleted today for failing that test.

`WITHOUT ROWID`, and why it is not a micro-optimisation
------------------------------------------------------
Each of these tables is *only* ever reached through its composite key, so an implicit
`rowid` would be a second key nobody queries and a second B-tree to store. Clustering the
row into the key it is looked up by measured at **30.3 -> 14.5 B/event, 6.3x -> 3.0x the
recording**, with identical build time and identical query latency. Halving the storage
for free is worth the two words.

Durability is deliberately off -- see `db.connect`, which owns the pragmas. A torn index
after a crash is not a data loss event, it is a rebuild, and the recording it derives from
is untouched.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3
"""Bumped whenever the DDL changes. A mismatch discards the index and rebuilds it.

2 (day 27): `files` and `exc_types` interning tables, `codes.filename` became `file_id`,
and `ix_frames_entry` was added for the live-at-seq range query.
3 (day 29): `exceptions` gained `chained_cause_seq`/`chained_context_seq` -- the recorded
`__cause__`/`__context__` object links (format 1.7, #11) -- so a chained exception is
traversable to its root, distinct from the existing `cause_seq` journey link."""

INDEXER_VERSION = 1
"""Bumped whenever an indexer's *output* changes for unchanged input -- a fixed bug, a new
column populated. Separate from `SCHEMA_VERSION` because the two drift independently: the
same tables filled in more correctly still means every existing index is wrong."""

DDL = """
-- Staleness detection and provenance. See `stamp` / `staleness`.
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;

-- Variable names, keyed by the recording's own `name_id`. Copied rather than
-- re-interned so an id means the same thing in the index and in the events, which is
-- what lets `var_writes.name_id` be looked up directly (ADR-0008 section 7).
CREATE TABLE strings(id INTEGER PRIMARY KEY, text TEXT NOT NULL);
--   query: "every write to `total`" -- the user types text, the index speaks ids.
CREATE INDEX ix_strings_text ON strings(text);

-- Source files, interned. Their own id space, not `strings`, because `strings.id` *is*
-- the recording's `name_id` and a shared pool would need a mapping table just to keep the
-- text lookup direct. Interning here earns its place for one reason: `line_hits` has a
-- row per executed line -- millions -- and each carries a `file_id` rather than a path.
CREATE TABLE files(file_id INTEGER PRIMARY KEY, path TEXT NOT NULL);
--   query: "every hit of this file:line" -- the user names a file, the index stores ids.
CREATE INDEX ix_files_path ON files(path);

-- Exception type names ("ValueError"), keyed by the recording's `exc_type_id`.
CREATE TABLE exc_types(id INTEGER PRIMARY KEY, text TEXT NOT NULL);

-- One row per interned code object. Never anything requiring the original .pyc: a
-- recording must be readable on a machine that has never seen the program.
CREATE TABLE codes(
    code_id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    qualname TEXT NOT NULL,
    first_lineno INTEGER NOT NULL
);

-- query: "every write to x, in order"        -> WHERE name_id=? ORDER BY seq
-- query: "the last write to x before S"      -> WHERE name_id=? AND seq<? ORDER BY seq DESC LIMIT 1
--        O(log n), a covering-index seek; this is the query the demo turns on.
CREATE TABLE var_writes(
    name_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    frame_id INTEGER NOT NULL,
    value_ref INTEGER,
    PRIMARY KEY (name_id, seq)
) WITHOUT ROWID;

-- query: "every hit of file:line"            -> WHERE file_id=? AND lineno=? ORDER BY seq
--        Retroactive breakpoints (day 30). O(log n + hits).
CREATE TABLE line_hits(
    file_id INTEGER NOT NULL,
    lineno INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (file_id, lineno, seq)
) WITHOUT ROWID;

-- The call tree. `parent_frame_id` lives here and *only* here: it is not part of
-- `ProgramState`, because a frame that entered before a keyframe has no recoverable
-- parent from that keyframe alone, which made it path-dependent (ADR-0006 amendment).
-- A single forward pass over the events knows it exactly.
CREATE TABLE frames(
    frame_id INTEGER PRIMARY KEY,
    code_id INTEGER NOT NULL,
    parent_frame_id INTEGER,
    entry_seq INTEGER NOT NULL,
    exit_seq INTEGER,
    exit_kind INTEGER
);
--   query: "the children of frame F, in call order"  -> the call tree, one level at a time
CREATE INDEX ix_frames_parent ON frames(parent_frame_id, entry_seq);
--   query: "which frames were live at seq S" -> entry_seq <= S AND (exit_seq > S OR NULL)
--          A covering range scan. See `call_tree.py` on why this works for liveness even
--          though the same intervals do NOT encode ancestry.
CREATE INDEX ix_frames_entry ON frames(entry_seq, exit_seq);
--   query: "every invocation of function F"          -> day 29's "who called this?"
CREATE INDEX ix_frames_code ON frames(code_id, entry_seq);

-- query: "every exception of type T"         -> WHERE type_id=? ORDER BY seq
-- query: "where did this one originate?"     -> follow cause_seq to the row with is_origin
-- query: "walk this exception's chain to its root" -> follow chained_cause_seq /
--        chained_context_seq (the recorded __cause__/__context__ links, day 29). These are
--        the exception-*object* chain (`raise X from Y`); `cause_seq` is a single
--        exception's propagation journey. Different questions, deliberately separate columns.
CREATE TABLE exceptions(
    seq INTEGER PRIMARY KEY,
    type_id INTEGER NOT NULL,
    frame_id INTEGER NOT NULL,
    is_origin INTEGER NOT NULL,
    cause_seq INTEGER,
    chained_cause_seq INTEGER,
    chained_context_seq INTEGER
);
CREATE INDEX ix_exceptions_type ON exceptions(type_id, seq);

-- query: "the scrubber background" -- events per time bucket, one row per timeline pixel.
--        Precomputed because the alternative is reading the whole recording to draw a
--        background. A full scan of a fixed, tiny table.
CREATE TABLE density(
    bucket INTEGER PRIMARY KEY,
    first_seq INTEGER NOT NULL,
    event_count INTEGER NOT NULL
) WITHOUT ROWID;
"""


def create(connection: sqlite3.Connection) -> None:
    """Apply the DDL to a fresh database. Idempotent only on an empty one.

    Pragmas are `db.connect`'s job, not this one's -- they are a property of the
    connection, not the schema, and setting them in both places is one list to drift.
    """
    connection.executescript(DDL)


def fingerprint(recording: Path) -> str:
    """Identify a recording cheaply enough to check on every open.

    Hashes the **header, the trailing 32 bytes and the size** -- not the whole file. A
    10 GB recording would take seconds to hash in full, and this check runs before every
    query, so an O(size) fingerprint would cost more than the queries it guards.

    Those three cover every realistic change: the tail is the EOCD, which carries the
    INDEX block's offset, length and CRC and therefore changes whenever any block does;
    the size catches truncation and append; the header catches a format change. A
    deliberately crafted collision is possible and out of scope -- this is a cache
    validity check, not a security boundary.

    Complexity: O(1) -- two reads and a 96-byte hash, whatever the recording's size.
    """
    size = recording.stat().st_size
    with recording.open("rb") as handle:
        head = handle.read(32)
        handle.seek(max(0, size - 32))
        tail = handle.read(32)
    return hashlib.blake2b(head + tail + str(size).encode(), digest_size=16).hexdigest()


def stamp(connection: sqlite3.Connection, recording: Path, event_count: int) -> None:
    """Record what this index was built from, so a later open can tell if it still fits."""
    connection.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        [
            ("schema_version", str(SCHEMA_VERSION)),
            ("indexer_version", str(INDEXER_VERSION)),
            ("recording_fingerprint", fingerprint(recording)),
            ("event_count", str(event_count)),
        ],
    )


def staleness(connection: sqlite3.Connection, recording: Path) -> str | None:
    """Why this index cannot be trusted, or None if it can.

    Returns a reason rather than a bool because the caller logs it: "rebuilt (indexer
    version changed)" is actionable and "rebuilt" is not.

    A missing `meta` table counts as stale -- that is what a half-written index from an
    interrupted build looks like, and durability is off precisely because the answer to
    that is a rebuild.
    """
    try:
        rows = dict(connection.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.DatabaseError:
        return "index is unreadable or was never finished"
    if rows.get("schema_version") != str(SCHEMA_VERSION):
        return f"schema version {rows.get('schema_version')} != {SCHEMA_VERSION}"
    if rows.get("indexer_version") != str(INDEXER_VERSION):
        return f"indexer version {rows.get('indexer_version')} != {INDEXER_VERSION}"
    if rows.get("recording_fingerprint") != fingerprint(recording):
        return "the recording changed since this index was built"
    return None
