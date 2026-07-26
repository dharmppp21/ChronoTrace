"""Turn a batch of tailed events into one bounded WebSocket frame -- the backpressure policy.

Problem this solves: the tailer hands the stream loop however many events appeared since the
last poll -- one at 1 event/minute, a hundred thousand at 100k/sec. Forwarding them raw is two
bugs: one WebSocket message per event drowns the browser, and an unbounded send queue behind a
slow client is a server memory leak. This module is the answer to both: it *aggregates* a batch
into a fixed-shape frame -- a compact density delta plus a capped list of notable events -- so a
frame's size is bounded by policy, not by how far behind the client is.

Interface: `frame(events, ...)` (pure aggregation) and the loop's tuning constants.

It must never know: sockets, sessions, files. It takes events and returns a DTO.

Drop-and-summarise, and why it is correct here
----------------------------------------------
Backpressure drops *frames*, never *data*. The `.chrono` file is the source of truth and stays
complete on disk; the live stream is a preview the user scrubs properly the moment recording
ends. So when more notable events arrive than a frame carries, the overflow becomes a `dropped`
count rather than an unbounded queue -- density still counts every event, so the *shape* is
never wrong, only the per-event markers are sampled. Dropping frames from a live view is
correct; dropping data from the recording never is, and this module only ever does the former.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chronotrace.recorder.events import EventKind
from chronotrace.server import dto
from chronotrace.store.tailer import TailState

if TYPE_CHECKING:
    from collections.abc import Iterable

    from chronotrace.recorder.events import Event

POLL_INTERVAL = 0.1
"""Seconds between polls -- and therefore the time-based batch window. One frame per tick caps
the browser's update rate; a burst in a tick is aggregated, not sent event by event."""

STALE_TIMEOUT = 5.0
"""No growth for this long with no footer => the writer is presumed dead. Long enough to
outlast a program blocked on I/O for a few seconds, short enough that a real crash is reported
promptly rather than leaving the stream hanging forever."""

BUCKET_WIDTH = 4096
"""Events per density bucket (~one EVENTS block). Coarse on purpose: the live view needs the
shape of activity, and per-instant detail is a `/state` call away once the playhead parks."""

MAX_NOTABLE = 64
"""Notable events carried per frame before the rest become a `dropped` summary. Bounds a
frame's size even when a tick spans an exception storm."""

_NOTABLE = frozenset({EventKind.RAISE})
"""Which kinds earn a live marker. Exceptions being born, only -- rare, and the one thing a
watcher wants to see the instant it happens. Calls are density, not markers (they are legion)."""

_STATE = {
    TailState.RUNNING: dto.StreamState.RUNNING,
    TailState.COMPLETE: dto.StreamState.COMPLETE,
    TailState.TRUNCATED: dto.StreamState.TRUNCATED,
}


def frame(
    events: Iterable[Event], *, total: int, state: TailState, dropped: int = 0
) -> dto.StreamFrame:
    """Aggregate a batch into one bounded `StreamFrame`: density + capped notable + a summary.

    Density counts every event into its bucket (complete but compact); notable exceptions are
    listed up to `MAX_NOTABLE` and the overflow folds into `dropped`. `total` is the running
    event count and `state` the tailer's verdict, mapped to its wire enum.
    """
    buckets: dict[int, int] = {}
    notable: list[dto.NotableEvent] = []
    overflow = 0
    for event in events:
        bucket = (event.seq // BUCKET_WIDTH) * BUCKET_WIDTH
        buckets[bucket] = buckets.get(bucket, 0) + 1
        if event.kind in _NOTABLE:
            if len(notable) < MAX_NOTABLE:
                notable.append(
                    dto.NotableEvent(
                        seq=event.seq, kind=event.kind.name.lower(), lineno=event.lineno
                    )
                )
            else:
                overflow += 1
    density = tuple(
        dto.DensityDelta(first_seq=b, event_count=c) for b, c in sorted(buckets.items())
    )
    return dto.StreamFrame(
        total_events=total,
        state=_STATE[state],
        density=density,
        notable=tuple(notable),
        dropped=dropped + overflow,
    )
