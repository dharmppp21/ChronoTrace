"""Makes the past **queryable**: "every write to `total`", "where did this exception start?".

Reconstruction (phase 3) answers *"what was the state at instant S?"*. That is one
question at a time, and it is the wrong shape for the questions people actually ask while
debugging, which are about the whole timeline at once: *when* did this change, *who* wrote
it, *where* did it come from. Answering those by replaying the recording is O(events) per
question. This layer precomputes them into a SQLite sidecar so each is a B-tree lookup.

**The index is derived state and is never authoritative.** Every fact in it comes from the
`.chrono`, which is the only source of truth. That single rule buys a great deal:

* it can be **deleted** at any time -- the worst outcome is a rebuild;
* it can be **rebuilt** from the recording alone, byte-for-byte identical;
* it can be **wrong about nothing**, because a stale one is detected (the recording's
  fingerprint and the indexer's version are stamped into it) and discarded rather than
  trusted;
* durability can be relaxed to the floor -- there is nothing here that a crash can lose
  that a rebuild cannot recreate.

If a query cannot be answered from the index, the honest fallback is to read the
recording, slowly. If a query *disagrees* with the recording, the index is wrong by
definition.

What this layer owns
--------------------
The schema, the indexers that populate it, and the staleness rule. It reads a recording
through `store`'s typed surface and writes SQLite; it computes nothing that the events do
not already contain.

What it must never know
-----------------------
Anything above it -- `reconstruct`, `query`, `server` (the dependency arrow points down).
It also never *interprets*: it does not decide what a query means or how a result is
rendered. It stores `seq` numbers, and `seq` is the address every layer above already
speaks. In particular it stores **pointers, not events**: the events stay in the
`.chrono`, which is what keeps the index a few bytes per event instead of a second copy of
the recording.

Design: [ADR-0008](../../../docs/adr/0008-index-schema.md). Built day 26.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from chronotrace.index.indexer import Cancelled, Indexer, Progress, Result, build
from chronotrace.index.var_writes import VarWriteIndexer, last_write_before, writes_to
from chronotrace.store import ChronoReader, Strings

__all__ = [
    "Cancelled",
    "Indexer",
    "Progress",
    "Result",
    "VarWriteIndexer",
    "build_index",
    "last_write_before",
    "writes_to",
]


def build_index(
    recording: Path,
    reader: ChronoReader,
    *,
    on_progress: Callable[[Progress], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Result:
    """Build every index this layer knows about, in one pass over the recording.

    The registry of indexers lives here rather than in the driver, so `indexer.py` stays
    ignorant of what any particular index stores. Day 27 adds three more to this list and
    changes nothing else.
    """

    def make(connection: sqlite3.Connection, _strings: Strings) -> list[Indexer]:
        return [VarWriteIndexer(connection)]

    return build(recording, reader, make, on_progress=on_progress, should_cancel=should_cancel)
