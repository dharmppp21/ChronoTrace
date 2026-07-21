"""Opening, batching and closing the sidecar database -- and why its durability is off.

Problem this solves: bulk-loading millions of rows into SQLite is *catastrophically*
slow if done naively, and the naive version is what you get by default. One `INSERT` per
event is one transaction per event, which means one fsync per event.

Interface: `connect` (a configured connection), `Batcher` (accumulate rows, flush every
N), `swap_into_place` (atomic publish).

It must never know: what an index *means*. It moves rows; `indexer.py` decides which.

Relaxed durability is correct here, specifically
------------------------------------------------
`synchronous=OFF` and `journal_mode=OFF` would be reckless for a user's data. They are
right for this file, and the reason is ADR-0008's decision paying dividends: **the index
is derived state.** Every row in it comes from the `.chrono`, which is untouched. If a
power cut corrupts the index, the recovery procedure is `rm` and rebuild -- there is
nothing here that a crash can destroy which the recording cannot recreate.

That is worth stating explicitly, because "turn off durability for speed" is normally a
bad trade made by someone who has not thought about it. Here it is the whole point of
having decided, on day 25, that the index is never authoritative. The cost of being wrong
is bounded and known: a rebuild, measured in seconds.

Build to a temporary file, then rename
--------------------------------------
Two processes may index the same recording at once, and a half-written index must never
be visible. Each builds its own temp file and `os.replace`s it into position -- the same
atomic-swap discipline `store/recovery.py` uses for `repair`. The loser's work is wasted,
never corrupt.

Locking was rejected: it adds a failure mode (a stale lock file blocks every future query,
and cleaning it up correctly across crashes is its own project) to save a few seconds of
duplicated work on a rare race.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

BATCH_ROWS = 10_000
"""Rows per transaction. Large enough that per-transaction overhead disappears into the
per-row cost, small enough that a batch's memory is bounded regardless of recording size
-- which matters because a 10 GB recording must not be indexed by first materialising it."""


def connect(path: Path) -> sqlite3.Connection:
    """Open the sidecar with bulk-load pragmas applied. See the module docstring.

    `journal_mode=OFF` removes rollback journalling entirely: a crash mid-build leaves a
    file that fails `staleness` and is rebuilt, so paying to make it recoverable buys
    nothing. `synchronous=OFF` stops waiting for the platform to confirm writes.
    `cache_size` is negative, which SQLite reads as KiB rather than pages.
    """
    connection = sqlite3.connect(path)
    connection.executescript(
        "PRAGMA journal_mode=OFF;"
        "PRAGMA synchronous=OFF;"
        "PRAGMA temp_store=MEMORY;"
        "PRAGMA cache_size=-65536;"  # 64 MiB, so B-tree creation is not I/O-bound
    )
    return connection


class Batcher:
    """Accumulates rows for one table and flushes them `BATCH_ROWS` at a time.

    One `executemany` per batch inside one transaction, rather than one `INSERT` per row.
    Use as a context manager so the final partial batch is never lost -- forgetting a
    flush would silently drop up to `BATCH_ROWS` events from the index, which is the kind
    of bug that shows up as a query that is quietly missing its last few answers.
    """

    __slots__ = ("_connection", "_pending", "_size", "_sql", "rows")

    def __init__(self, connection: sqlite3.Connection, sql: str, *, size: int = BATCH_ROWS) -> None:
        self._connection = connection
        self._sql = sql
        self._size = size
        self._pending: list[Sequence[Any]] = []
        self.rows = 0

    def add(self, row: Sequence[Any]) -> None:
        """Queue one row, flushing automatically once a batch is full."""
        self._pending.append(row)
        if len(self._pending) >= self._size:
            self.flush()

    def flush(self) -> None:
        """Write and clear the pending batch. Safe to call on an empty one."""
        if not self._pending:
            return
        self._connection.executemany(self._sql, self._pending)
        self.rows += len(self._pending)
        self._pending.clear()

    def __enter__(self) -> Batcher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.flush()  # on the error path the whole index is discarded anyway


def swap_into_place(temporary: Path, destination: Path) -> None:
    """Publish a finished index atomically, replacing any previous one.

    `os.replace` is atomic on POSIX and on Windows, so a reader either sees the old index
    or the new one -- never a partial file. This is what makes concurrent builders safe
    without a lock.

    **Windows caveat, and it is real:** the swap fails with `PermissionError` if another
    process still has the destination *open*, because SQLite does not open with
    `FILE_SHARE_DELETE`. POSIX has no such restriction. The failure is safe -- the
    recording is untouched and the old index stays valid -- but it means a rebuild can
    lose to a live reader on Windows, and the caller has to close its connections first.
    Tracked as issue #10; the day-33 server will need to reopen rather than hold.

    Raises:
        OSError: the swap could not happen. Nothing is published; the temporary file is
            left for the caller to discard.
    """
    temporary.replace(destination)


def sidecar_path(recording: Path) -> Path:
    """Where `recording`'s index lives: beside it, or in a cache directory if it cannot be.

    Reading someone else's recording out of a read-only share is ordinary, and it must not
    be a hard failure -- so an unwritable directory falls back to the user's cache,
    keyed by the recording's name and its parent, which is enough to keep two recordings
    called `run.chrono` apart.

    Rejected: always using the cache directory. Keeping the index beside the recording
    means deleting the recording's folder cleans up after itself, and it is where a user
    would look for it.
    """
    beside = recording.with_suffix(recording.suffix + ".idx")
    if os.access(recording.parent, os.W_OK):
        return beside
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    cache = Path(root) if root else Path.home() / ".cache"
    return cache / "chronotrace" / f"{recording.parent.name}-{beside.name}"
