"""Timeline density, and the conservation property that catches bucket-edge bugs.

Every event lands in exactly one bucket, so the buckets must sum to the event count. An
off-by-one at a bucket edge shows up here and nowhere else -- the scrubber would just draw
a slightly wrong picture nobody could distinguish from the truth.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from chronotrace.index import profile, schema
from chronotrace.index.density import BUCKETS, DensityIndexer
from chronotrace.recorder.events import Event, EventKind

from .conftest import Indexed, index_example


def _drive(total: int) -> sqlite3.Connection:
    """Feed `total` synthetic events through the indexer, directly.

    Driven rather than recorded because the property under test is the bucket arithmetic,
    not the recorder -- and reaching many-events-per-bucket by recording would mean
    recording tens of thousands of events per run.
    """
    db = sqlite3.connect(":memory:")
    schema.create(db)
    indexer = DensityIndexer(db, total)
    for seq in range(total):
        indexer.consume(
            Event(
                seq=seq,
                kind=EventKind.LINE,
                timestamp_ns=seq,
                thread_id=1,
                frame_id=1,
                code_id=0,
            )
        )
    indexer.finalise()
    return db


def test_buckets_sum_to_the_event_count(simple: Indexed) -> None:
    assert sum(count for _b, _f, count in profile(simple.db)) == len(simple.events)


def test_conservation_holds_when_many_events_share_a_bucket() -> None:
    """The case the arithmetic is for. Deliberately not a multiple of `BUCKETS`, so the
    last bucket is ragged -- which is exactly where an edge bug hides."""
    total = BUCKETS * 7 + 3
    rows = profile(_drive(total))
    assert sum(count for _b, _f, count in rows) == total
    assert len(rows) <= BUCKETS


def test_buckets_never_exceed_the_fixed_resolution(simple: Indexed) -> None:
    """At most one row per timeline pixel, whatever the recording's size."""
    rows = profile(simple.db)
    assert 0 < len(rows) <= BUCKETS
    assert all(0 <= bucket < BUCKETS for bucket, _f, _c in rows)


def test_buckets_and_their_jump_targets_both_rise(simple: Indexed) -> None:
    """`first_seq` is what a click on the timeline jumps to, so it must rise with the
    bucket -- otherwise clicking further right scrubs backwards."""
    rows = profile(simple.db)
    assert [b for b, _f, _c in rows] == sorted(b for b, _f, _c in rows)
    assert [f for _b, f, _c in rows] == sorted(f for _b, f, _c in rows)


def test_an_empty_recording_produces_an_empty_table() -> None:
    """No rows rather than 2048 zeros: "no row" and "count 0" mean the same to a renderer
    that draws what it is given, and one of them costs 2048 writes."""
    db = sqlite3.connect(":memory:")
    schema.create(db)
    DensityIndexer(db, 0).finalise()
    assert profile(db) == []


def test_a_real_recording_reads_as_a_shape_not_just_a_sum(tmp_path: Path) -> None:
    """Conservation alone would pass on a flat, wrong picture. A program with a loop must
    show at least one bucket denser than another."""
    indexed = index_example(tmp_path, "simple")
    counts = [count for _b, _f, count in profile(indexed.db)]
    assert sum(counts) == len(indexed.events)
    assert len(set(counts)) > 1 or len(counts) == len(indexed.events)
