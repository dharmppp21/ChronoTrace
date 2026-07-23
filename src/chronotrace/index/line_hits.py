"""Every execution of every line -- retroactive breakpoints, and the source heatmap.

Problem this solves: *"stop wherever line 47 ran"* — except the program already finished,
so there is nothing to stop. A retroactive breakpoint is a **query**: the list of instants
where that line executed, jumpable in either direction. Day 30 builds the command; this
builds the answer.

Interface: `LineHitIndexer` (the `Indexer` protocol) plus `hits_of` and `heatmap`.

It must never know: what a breakpoint *is*, or how a heatmap is coloured. It stores `seq`
numbers against `(file, line)` and stops.

Why `file_id` rather than `code_id`
-----------------------------------
A user sets a breakpoint on `pipeline.py:47`, not on a code object -- they are reading
source, and the source is a file. Storing `code_id` would make the common query a join
through `codes`, and would split one source line across every code object that spans it
(a comprehension, a nested `def`). `files` interns the path so a row costs an integer
rather than a path string, which matters here specifically: this is the largest table in
the index, one row per executed line.

The heatmap is a `GROUP BY`, and is deliberately not materialised
-----------------------------------------------------------------
*"How many times did each line in this file run?"* is
`SELECT lineno, count(*) ... WHERE file_id=? GROUP BY lineno`. The obvious optimisation is
a second table holding those counts.

Measured rather than assumed: **12.8 ms** over 178,914 rows on a 281k-event recording.
That is fine for what it is -- the background is drawn once when a file is opened, not on
every repaint -- but it is over a 60 fps frame budget, so a UI that recomputed it while
scrolling would stutter, and at 10M events it extrapolates to roughly half a second.

Not materialised **today**, because nothing consumes it until day 35 and a second table
would be a second representation of one fact, free to disagree. The decision is recorded
with its real number and its breaking point (issue #12) so day 35 inherits a measurement
rather than a hunch -- which is the opposite of what an unchecked "it is fast enough"
would have left behind.
"""

from __future__ import annotations

import sqlite3

from chronotrace.index.db import Batcher
from chronotrace.recorder.events import Event, EventKind

INSERT = "INSERT OR REPLACE INTO line_hits(file_id, lineno, seq) VALUES (?,?,?)"


class LineHitIndexer:
    """Turns `LINE` events into `line_hits` rows. Satisfies the `Indexer` protocol.

    Holds a `code_id -> file_id` map built from the recording's own tables, so the hot
    loop is a dict lookup rather than a join.
    """

    __slots__ = ("_batch", "_file_of_code")

    def __init__(self, connection: sqlite3.Connection, file_of_code: dict[int, int]) -> None:
        self._batch = Batcher(connection, INSERT)
        self._file_of_code = file_of_code

    def consume(self, event: Event) -> None:
        """Record a line execution.

        A `code_id` with no known file is skipped rather than guessed: `exec`'d code
        reports `<string>` and belongs to no file a user can open, so a breakpoint on it
        could never be set and a row for it would only inflate the largest table.
        """
        if event.kind is not EventKind.LINE:
            return
        file_id = self._file_of_code.get(event.code_id)
        if file_id is None:
            return
        self._batch.add((file_id, event.lineno, event.seq))

    def finalise(self) -> None:
        """Flush the last partial batch. The clustered PK is the index; nothing to build."""
        self._batch.flush()


def hits_of(connection: sqlite3.Connection, file_id: int, lineno: int) -> list[int]:
    """Every `seq` at which `file_id:lineno` executed, in order. A retroactive breakpoint.

    Complexity: O(log n + hits) -- a range scan over the clustered key, already sorted by
    `seq`, so "the next hit after S" and "the previous hit before S" are both free.
    """
    return [
        row[0]
        for row in connection.execute(
            "SELECT seq FROM line_hits WHERE file_id=? AND lineno=? ORDER BY seq", (file_id, lineno)
        )
    ]


def next_hit(connection: sqlite3.Connection, file_id: int, lineno: int, after: int) -> int | None:
    """The first hit of `file_id:lineno` strictly after `after` -- "continue to breakpoint".

    Complexity: O(log n). Does not materialise the hit list, which matters for a line
    inside a hot loop where `hits_of` would return a million rows to use one.
    """
    row = connection.execute(
        "SELECT seq FROM line_hits WHERE file_id=? AND lineno=? AND seq>? ORDER BY seq LIMIT 1",
        (file_id, lineno, after),
    ).fetchone()
    return int(row[0]) if row is not None else None


def previous_hit(
    connection: sqlite3.Connection, file_id: int, lineno: int, before: int
) -> int | None:
    """The last hit strictly before `before` -- **reverse**-continue to a breakpoint.

    The mirror of `next_hit`, and the reason day 21's stepping symmetry extends to
    breakpoints: the same clustered key answers both directions in O(log n).
    """
    row = connection.execute(
        "SELECT seq FROM line_hits WHERE file_id=? AND lineno=? AND seq<? "
        "ORDER BY seq DESC LIMIT 1",
        (file_id, lineno, before),
    ).fetchone()
    return int(row[0]) if row is not None else None


def heatmap(connection: sqlite3.Connection, file_id: int) -> dict[int, int]:
    """`lineno -> execution count` for one file. The source pane's background.

    Computed rather than stored: see the module docstring, which carries the measurement
    (12.8 ms over 178,914 rows on a 281k-event recording) and the reason it is not
    materialised (issue #12).

    Complexity: O(rows for this file), one contiguous scan with no sort.
    """
    return {
        int(lineno): int(count)
        for lineno, count in connection.execute(
            "SELECT lineno, count(*) FROM line_hits WHERE file_id=? GROUP BY lineno", (file_id,)
        )
    }
