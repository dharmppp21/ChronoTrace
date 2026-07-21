"""One pass over the recording, feeding every indexer at once.

Problem this solves: there will be four indexers by day 27 -- variable writes, line hits,
the call tree, exceptions -- and a recording can be larger than RAM. Four indexers must
not mean four reads. `build` streams the events once and hands each to every registered
indexer, so adding the fifth costs one more `consume` call per event and **zero** extra
I/O.

Interface: the `Indexer` protocol (`consume`, `finalise`) and `build`.

It must never know: what any particular indexer stores, or SQL beyond the schema module.

Why this abstraction is earned, when most are not
--------------------------------------------------
The project's rule is that an interface with one implementation is a liability. This one
has a **dated second caller**: day 27 adds the line-hit, call-tree and exception indexers,
and the ADR-0008 schema already has their tables. The alternative -- writing the variable
indexer inline today and extracting a protocol tomorrow -- would mean rewriting today's
driver on day 27 with three more things depending on it. That is the case where building
the seam up front is cheaper, and it is narrow: two methods, no configuration, no
registry, no plugin discovery.

Progress and cancellation are requirements, not decorations
-----------------------------------------------------------
Indexing ten million events takes real seconds. An uncancellable stall with no feedback is
how a tool gets uninstalled -- the user cannot tell "working" from "hung", and their only
recourse is to kill it. So `build` reports progress on a cadence and checks a cancel
signal on the same cadence, which the day-33 server will drive from a WebSocket and the
CLI drives from a terminal line. Cancelling leaves the temporary file behind and never
publishes it, so a cancelled build is indistinguishable from one that never ran.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from chronotrace.index import db, schema
from chronotrace.recorder.events import Event
from chronotrace.store import ChronoReader, Strings

PROGRESS_EVERY = 50_000
"""Events between progress callbacks and cancellation checks. Frequent enough that a
cancel feels immediate (~0.08 s of work at the measured rate), rare enough that the check
itself never shows up in a profile."""


class Indexer(Protocol):
    """Turns events into rows. One instance per build; never reused across recordings."""

    def consume(self, event: Event) -> None:
        """Fold one event in, in `seq` order. Must not raise on an event it ignores."""
        ...

    def finalise(self) -> None:
        """Flush anything buffered. Called once, after the last event, before commit."""
        ...


class Cancelled(Exception):
    """The caller asked for the build to stop. The temporary index is discarded unread."""


@dataclass(frozen=True, slots=True)
class Progress:
    """How far a build has got. `total` is the recording's event count, known up front."""

    done: int
    total: int

    @property
    def fraction(self) -> float:
        """Completion in [0, 1]. An empty recording is complete, not undefined."""
        return self.done / self.total if self.total else 1.0


@dataclass(frozen=True, slots=True)
class Result:
    """What a finished build produced, for the caller to report."""

    path: Path
    events: int
    rows: int
    partial: bool


def build(
    recording: Path,
    reader: ChronoReader,
    make_indexers: Callable[[sqlite3.Connection, Strings], list[Indexer]],
    *,
    on_progress: Callable[[Progress], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Result:
    """Index `reader` into a sidecar beside `recording`, atomically.

    Builds into a temporary file and swaps it in at the end, so a concurrent builder or a
    crash can never leave a half-index visible (`db.swap_into_place`).

    Args:
        recording: the `.chrono` path, used for the sidecar location and the fingerprint.
        reader: the open recording. A truncated one indexes its recovered prefix.
        make_indexers: builds the indexers, given the connection and the recording's
            intern tables. A factory rather than a list because each indexer holds a
            `Batcher` bound to *this* connection.
        on_progress: called every `PROGRESS_EVERY` events.
        should_cancel: polled on the same cadence.

    Returns:
        Where the index landed, and whether it covers a truncated recording.

    Raises:
        Cancelled: `should_cancel` returned True. Nothing is published.

    Complexity: O(events) time, one pass; memory bounded by `db.BATCH_ROWS` per indexer,
    not by the recording's size.
    """
    destination = db.sidecar_path(recording)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)

    connection = db.connect(temporary)
    try:
        schema.create(connection)
        strings = reader.strings()
        _load_intern_tables(connection, strings)
        indexers = make_indexers(connection, strings)
        total = len(reader)
        done = _stream(reader, indexers, total, on_progress, should_cancel)
        for indexer in indexers:
            indexer.finalise()
        schema.stamp(connection, recording, event_count=done)
        connection.commit()
        rows = _row_count(connection)
    finally:
        connection.close()

    db.swap_into_place(temporary, destination)
    return Result(path=destination, events=total, rows=rows, partial=reader.truncated)


def _load_intern_tables(connection: sqlite3.Connection, strings: Strings) -> None:
    """Copy the recording's own intern tables in, before the event pass.

    Not an `Indexer`: these are not derived from events, they arrive whole from the
    STRINGS block (format 1.6). Loaded first so a query can resolve the text a user types
    to the id the rows store, which is the lookup every other query starts from.
    """
    connection.executemany(
        "INSERT OR REPLACE INTO strings(id, text) VALUES (?,?)", enumerate(strings.names)
    )
    connection.executemany(
        "INSERT OR REPLACE INTO codes(code_id, filename, qualname, first_lineno) VALUES (?,?,?,?)",
        ((i, c.filename, c.qualname, c.first_lineno) for i, c in enumerate(strings.codes)),
    )


def _stream(
    reader: ChronoReader,
    indexers: list[Indexer],
    total: int,
    on_progress: Callable[[Progress], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> int:
    """The single pass. Iterating the reader keeps memory bounded over a 10 GB file."""
    done = 0
    # Checked before any work as well as on the cadence: a recording shorter than
    # PROGRESS_EVERY would otherwise be uncancellable, and "cancel was ignored because
    # the job was small" is a rule nobody can predict from the outside.
    _check_cancel(should_cancel, done, total)
    for event in reader.iter_events():
        for indexer in indexers:
            indexer.consume(event)
        done += 1
        if done % PROGRESS_EVERY == 0:
            _check_cancel(should_cancel, done, total)
            if on_progress is not None:
                on_progress(Progress(done, total))
    if on_progress is not None:
        on_progress(Progress(done, total))
    return done


def _check_cancel(should_cancel: Callable[[], bool] | None, done: int, total: int) -> None:
    if should_cancel is not None and should_cancel():
        raise Cancelled(f"indexing cancelled after {done:,} of {total:,} events")


def _row_count(connection: sqlite3.Connection) -> int:
    """Total indexed rows, for the caller's report. Cheap: these are small tables."""
    # The table names are literals from the tuple below, never caller input, so the
    # interpolation cannot carry SQL from anywhere a user controls.
    return sum(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in ("var_writes", "line_hits", "frames", "exceptions")
    )
