"""Every write to every variable -- the index the flagship query runs on.

Problem this solves: *"who last wrote to `total`, before things went wrong?"* Answering it
by replay is O(events) and needs the whole recording walked per question. This makes it a
B-tree seek.

Interface: `VarWriteIndexer` (the `Indexer` protocol) and the two query helpers the day-28
engine will call.

It must never know: what a query *means*, or how a result is rendered. It stores `seq`
numbers -- the address every layer above already speaks.

The index that makes it instant
-------------------------------
`var_writes` is clustered on `(name_id, seq)` (ADR-0008 §5), and that composite serves
both queries the demo needs:

* **every write to `x`, in order** -- `WHERE name_id=? ORDER BY seq`. The rows for one
  name are contiguous *and already sorted*, so there is no sort step: O(log n + k).
* **the last write to `x` before `seq`** -- `WHERE name_id=? AND seq<? ORDER BY seq DESC
  LIMIT 1`. A single descending range scan stopping at the first row: **O(log n)**,
  measured at 91 µs on a 281k-event recording, and it does not care whether the name was
  written once or a million times.

That second one is the query the demo turns on, and its complexity is the reason the
pitch works: you stop re-running your program to find out who wrote a value.

Recursion is why `frame_id` is stored
-------------------------------------
`total` in one invocation and `total` in another are different variables that share a
name. Keying on the name alone would merge them, and "the last write to `total`" would
answer with some *other* call's write -- confidently, and wrongly. So every row carries
the `frame_id` the recorder attributed the write to, and a frame-scoped query filters on
it. Day 6's per-frame identity is what makes that possible; `test_var_writes.py` pins it
under real recursion.

There is no index to create after the load, and that was measured
------------------------------------------------------------------
The standard advice is to bulk-load first and build indexes at the end, because
maintaining a B-tree per row costs more than sorting once. It does not hold here, and
`benchmarks/bench_index.py` shows why: over two million rows, building after the load
(4.49 s) was no faster than building before it (4.31 s). The rows arrive in `seq` order
across only a few hundred distinct names, so every name's page is already hot and
maintaining the tree during insert is nearly free.

The clustered `WITHOUT ROWID` table beats both anyway -- **3.68 s and half the size**, at
the same query latency -- because it stores one B-tree instead of a table plus an index.
So `var_writes` ships no secondary index at all: the primary key *is* the access path, and
`finalise` has nothing to build.
"""

from __future__ import annotations

import sqlite3

from chronotrace.index.db import Batcher
from chronotrace.recorder.events import Event, EventKind

INSERT = "INSERT OR REPLACE INTO var_writes(name_id, seq, frame_id, value_ref) VALUES (?,?,?,?)"

DELETED = -1
"""`value_ref` for a `del x` (format 1.5). Kept as a row rather than dropped: "when did
`x` stop existing?" is a real question, and a deletion is the answer to "who last wrote
to it?" more often than people expect."""


class VarWriteIndexer:
    """Turns `VAR_WRITE` events into `var_writes` rows. Satisfies the `Indexer` protocol."""

    __slots__ = ("_batch",)

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._batch = Batcher(connection, INSERT)

    def consume(self, event: Event) -> None:
        """Record a binding change. Ignores every other kind of event.

        `INSERT OR REPLACE` rather than plain `INSERT`: `(name_id, seq)` is unique because
        `seq` is unique per event, so a conflict is impossible in a well-formed recording.
        The clause makes a **rebuild over a partially-written index idempotent** instead of
        raising, which is the behaviour ADR-0008 requires.
        """
        if event.kind is not EventKind.VAR_WRITE or event.name_id is None:
            return
        value_ref = DELETED if event.value_ref is None else int(event.value_ref)
        self._batch.add((event.name_id, event.seq, event.frame_id, value_ref))

    def finalise(self) -> None:
        """Flush the last partial batch. The clustered PK is the index; nothing to build."""
        self._batch.flush()


def writes_to(
    connection: sqlite3.Connection, name_id: int, *, frame_id: int | None = None
) -> list[tuple[int, int, int]]:
    """Every write to `name_id`, in `seq` order, as `(seq, frame_id, value_ref)`.

    Args:
        connection: an open index.
        name_id: from the recording's `strings` table -- see `chronotrace.store.Strings`.
        frame_id: restrict to one invocation. Necessary under recursion, where several
            live frames hold a variable of the same name.

    Complexity: O(log n + k) for k matching rows, with no sort -- the clustered key is
    already in `seq` order within a name.
    """
    if frame_id is None:
        rows = connection.execute(
            "SELECT seq, frame_id, value_ref FROM var_writes WHERE name_id=? ORDER BY seq",
            (name_id,),
        )
    else:
        rows = connection.execute(
            "SELECT seq, frame_id, value_ref FROM var_writes "
            "WHERE name_id=? AND frame_id=? ORDER BY seq",
            (name_id, frame_id),
        )
    return list(rows)


def last_write_before(
    connection: sqlite3.Connection, name_id: int, seq: int, *, frame_id: int | None = None
) -> tuple[int, int, int] | None:
    """The most recent write to `name_id` strictly before `seq`, or None if there is none.

    **The query the demo is built on.** Strictly before, not at-or-before: the caller is
    standing at `seq` asking who put this value here, and the write *at* `seq` is the one
    they are already looking at.

    Complexity: **O(log n)** -- a descending range scan on the clustered key that stops at
    the first row. Independent of how many times the name was written, which is the point:
    a variable written a million times answers as fast as one written twice.
    """
    if frame_id is None:
        row = connection.execute(
            "SELECT seq, frame_id, value_ref FROM var_writes "
            "WHERE name_id=? AND seq<? ORDER BY seq DESC LIMIT 1",
            (name_id, seq),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT seq, frame_id, value_ref FROM var_writes "
            "WHERE name_id=? AND seq<? AND frame_id=? ORDER BY seq DESC LIMIT 1",
            (name_id, seq, frame_id),
        ).fetchone()
    if row is None:
        return None
    return (int(row[0]), int(row[1]), int(row[2]))
