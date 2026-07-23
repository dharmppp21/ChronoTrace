"""Where exceptions began, and where they went -- day 29's raw material.

Problem this solves: *"where did this `ValueError` come from?"* A traceback shows the
frames it crossed; it does not show the instant it was born, what the locals were there,
or the twenty lines that ran before. The recording has all of that, and this index makes
the origin one lookup away from the symptom.

Interface: `ExceptionIndexer`, plus `of_type`, `origin_of` and `propagation_of`.

It must never know: what a "root cause" means. It records what the events say -- born
here, crossed there, caught there -- and lets day 29's query layer draw conclusions.

Origins are recorded, not inferred, because day 6 made that possible
--------------------------------------------------------------------
CPython's `sys.monitoring` fires `RAISE` in *every* frame an exception passes through, so
a naive index would mark three origins for one exception and answer "where did it come
from?" with whichever frame the user happened to be looking at. Day 6 measured that and
made the recorder emit `RAISE` **only** for an exception's first sighting, describing
propagation with `UNWIND` and `EXCEPTION_HANDLED` instead.

`is_origin` is therefore a fact copied from the event stream, not a heuristic applied to
it. That day-6 decision is what makes this file eight lines of bookkeeping instead of a
guess.

Two kinds of "cause", kept in separate columns on purpose
---------------------------------------------------------
`cause_seq` points from a propagation event (`UNWIND`, `EXCEPTION_HANDLED`) back to the
`RAISE` that started it, so "show me this exception's whole journey" is one indexed
lookup. It is maintained by tracking the exception currently in flight during the single
pass -- a stack, because an exception raised inside an `except` block is in flight while
the first one still is. This is *one* exception moving through frames.

`chained_cause_seq` / `chained_context_seq` are the different thing: Python's
`__cause__`/`__context__` chain (`raise X from Y`), which links two *distinct* exception
objects. Until day 29 the recorder never observed that link (issue #11), and this file's
docstring said so and refused to guess it from adjacency. Format 1.7 now records it -- the
recorder maps each exception's id to its origin RAISE and reads the links off the object at
raise time -- so these columns are a *recorded fact*, copied straight off the RAISE event,
not an inference. A chain is walked to its root by following `chained_cause_seq` (explicit
`from`) or, failing that, `chained_context_seq` (implicit), exactly as a Python traceback
prints "The above exception was the direct cause" vs "During handling of the above". Where
a link points into unrecorded code, it is None -- the chain genuinely ends outside the
recording, and the day-29 query says so rather than pointing at the wrong frame.
"""

from __future__ import annotations

import sqlite3

from chronotrace.index.db import Batcher
from chronotrace.recorder.events import Event, EventKind

INSERT = (
    "INSERT OR REPLACE INTO exceptions"
    "(seq, type_id, frame_id, is_origin, cause_seq, chained_cause_seq, chained_context_seq) "
    "VALUES (?,?,?,?,?,?,?)"
)

_EXCEPTION_KINDS = (EventKind.RAISE, EventKind.UNWIND, EventKind.EXCEPTION_HANDLED)


class ExceptionIndexer:
    """Turns the exception lifecycle into rows. Satisfies the `Indexer` protocol."""

    __slots__ = ("_batch", "_in_flight")

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._batch = Batcher(connection, INSERT)
        self._in_flight: list[int] = []

    def consume(self, event: Event) -> None:
        """Record a birth, a crossing, or a catch.

        The in-flight stack is what links a crossing to its origin. An exception raised
        while another is being handled pushes a second entry, and `EXCEPTION_HANDLED` pops
        the innermost -- which is the order CPython unwinds them in.
        """
        if event.kind not in _EXCEPTION_KINDS or event.exc_type_id is None:
            return
        origin = event.kind is EventKind.RAISE
        if origin:
            self._in_flight.append(event.seq)
        cause = self._in_flight[-1] if self._in_flight else None
        # The __cause__/__context__ links ride on the origin RAISE only (the recorder sets
        # them nowhere else); a propagation row carries None, which is correct -- a crossing
        # is not where a chain is decided.
        self._batch.add(
            (
                event.seq,
                event.exc_type_id,
                event.frame_id,
                int(origin),
                cause,
                event.exc_cause_seq,
                event.exc_context_seq,
            )
        )
        if event.kind is EventKind.EXCEPTION_HANDLED and self._in_flight:
            self._in_flight.pop()

    def finalise(self) -> None:
        """Flush the last partial batch.

        An exception still in flight at the end simply has no `HANDLED` row, which is
        exactly what an uncaught exception looks like and is worth being able to see.
        """
        self._batch.flush()


def of_type(connection: sqlite3.Connection, type_id: int) -> list[tuple[int, int, bool]]:
    """Every event for exceptions of `type_id`, as `(seq, frame_id, is_origin)`.

    Complexity: O(log n + hits) via `ix_exceptions_type`.
    """
    return [
        (int(seq), int(frame), bool(origin))
        for seq, frame, origin in connection.execute(
            "SELECT seq, frame_id, is_origin FROM exceptions WHERE type_id=? ORDER BY seq",
            (type_id,),
        )
    ]


def origin_of(connection: sqlite3.Connection, seq: int) -> tuple[int, int, int] | None:
    """Where the exception seen at `seq` was born, as `(seq, type_id, frame_id)`.

    **The day-29 flagship query.** The user is standing at a traceback frame; this jumps
    them to the instant the exception came into existence, where the locals that caused it
    are still reconstructable.

    Complexity: two O(log n) primary-key lookups -- the row at `seq`, then its origin.
    """
    row = connection.execute(
        "SELECT cause_seq, is_origin FROM exceptions WHERE seq=?", (seq,)
    ).fetchone()
    if row is None:
        return None
    origin_seq = seq if row[1] else row[0]
    if origin_seq is None:
        return None
    found = connection.execute(
        "SELECT seq, type_id, frame_id FROM exceptions WHERE seq=?", (origin_seq,)
    ).fetchone()
    return (int(found[0]), int(found[1]), int(found[2])) if found is not None else None


def propagation_of(connection: sqlite3.Connection, origin_seq: int) -> list[tuple[int, int, int]]:
    """The whole journey of the exception born at `origin_seq`, in order.

    Every frame it crossed and where it stopped, as `(seq, frame_id, kind_is_origin)` --
    which is the call stack of a traceback, except every entry is a jumpable instant.
    """
    return [
        (int(seq), int(frame), int(origin))
        for seq, frame, origin in connection.execute(
            "SELECT seq, frame_id, is_origin FROM exceptions "
            "WHERE seq=? OR cause_seq=? ORDER BY seq",
            (origin_seq, origin_seq),
        )
    ]
