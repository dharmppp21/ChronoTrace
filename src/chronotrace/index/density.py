"""Events per bucket, so the scrubber can draw a million-event timeline without reading it.

Problem this solves: the timeline's background shows *where the activity is* -- the dense
band that is a tight loop, the flat stretch that is an I/O wait. Computing it by counting
events per pixel means touching every event, on every repaint, for a picture that is a few
thousand numbers.

Interface: `DensityIndexer` and `profile`.

It must never know: pixels, colours, or zoom. It counts events into fixed buckets; the UI
decides what a bucket looks like.

Why a fixed bucket count rather than per-zoom queries
------------------------------------------------------
The resolution that matters is **one bucket per horizontal pixel**. A timeline is a few
thousand pixels wide at most (a 4K display, full width, is ~3800), so `BUCKETS = 2048`
gives a pixel-accurate background on any realistic window and rounds to a power of two.

The alternative -- aggregate on demand at whatever zoom the user is at -- was rejected on
what it costs at the only moment it matters. Zooming and dragging repaint continuously,
and each repaint would be a `GROUP BY` over the event table; the scrubber would get slower
exactly as the user interacts with it more. Precomputing makes every repaint a scan of
2048 rows, which is free and, more importantly, *constant* -- the timeline feels the same
on a 1,000-event recording and a 10,000,000-event one.

Zooming *in* past the bucket resolution is then a range query against the real tables, not
this one. That is the right split: this exists for the overview, where reading the whole
recording is unaffordable, and the overview is exactly where approximation is invisible.

Buckets are over `seq`, not time
---------------------------------
`seq` is the axis the scrubber scrubs and the address everything else speaks, so a bucket
maps to a draggable range directly. Wall-clock buckets would need a second mapping to get
back to an instant, and day 4 already established that timestamps are data rather than
identity -- two events can share one, so time does not order the recording. The
`first_seq` column is what a click on a bucket jumps to.
"""

from __future__ import annotations

import sqlite3

from chronotrace.recorder.events import Event

BUCKETS = 2048
"""Buckets across the whole recording. One per pixel at any realistic timeline width; see
the module docstring. A power of two so the arithmetic is exact and the last bucket is not
a ragged remainder."""

INSERT = "INSERT OR REPLACE INTO density(bucket, first_seq, event_count) VALUES (?,?,?)"


class DensityIndexer:
    """Counts events into `BUCKETS` fixed ranges. Satisfies the `Indexer` protocol.

    Takes the recording's event count up front, because a bucket's width is
    `total / BUCKETS` and that is not knowable from a single event. The driver has it --
    `len(reader)` -- so nothing needs to be buffered or measured twice.

    Memory is `BUCKETS` integers regardless of recording size, which is the point: this is
    the one index whose cost does not grow with the thing it describes.
    """

    __slots__ = ("_batch", "_counts", "_first", "_total", "_width")

    def __init__(self, connection: sqlite3.Connection, total: int) -> None:
        self._batch = connection
        self._total = total
        # A width of at least 1 keeps a recording smaller than BUCKETS from dividing by
        # zero; it then simply uses one bucket per event and the rest stay empty.
        self._width = max(1, -(-total // BUCKETS))
        self._counts = [0] * BUCKETS
        self._first: list[int | None] = [None] * BUCKETS

    def consume(self, event: Event) -> None:
        """Count one event into its bucket, remembering the first `seq` that landed there."""
        bucket = min(event.seq // self._width, BUCKETS - 1)
        self._counts[bucket] += 1
        if self._first[bucket] is None:
            self._first[bucket] = event.seq

    def finalise(self) -> None:
        """Write the non-empty buckets.

        Empty buckets are omitted rather than stored as zeros: a recording shorter than
        `BUCKETS` would otherwise write mostly-empty rows, and "no row" and "count 0" mean
        the same thing to a renderer that iterates what it is given.
        """
        rows = [
            (bucket, first, self._counts[bucket])
            for bucket, first in enumerate(self._first)
            if first is not None
        ]
        if rows:
            self._batch.executemany(INSERT, rows)


def profile(connection: sqlite3.Connection) -> list[tuple[int, int, int]]:
    """The whole timeline background, as `(bucket, first_seq, event_count)`.

    Complexity: O(BUCKETS) -- a scan of a table with at most 2048 rows, whatever the
    recording's size. Returned whole because the caller draws all of it.
    """
    return [
        (int(bucket), int(first), int(count))
        for bucket, first, count in connection.execute(
            "SELECT bucket, first_seq, event_count FROM density ORDER BY bucket"
        )
    ]
