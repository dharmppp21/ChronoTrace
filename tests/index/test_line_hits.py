"""Line hits: retroactive breakpoints in both directions, and the heatmap.

The breakpoint queries are what day 30 turns into a command, so they are checked against
the event stream event for event rather than by count.
"""

from __future__ import annotations

from pathlib import Path

from chronotrace.index import heatmap, hits_of, next_hit, previous_hit
from chronotrace.recorder.events import EventKind

from .conftest import Indexed, index_example


def _line_seqs(indexed: Indexed, lineno: int) -> list[int]:
    return [e.seq for e in indexed.events if e.kind is EventKind.LINE and e.lineno == lineno]


def test_every_hit_of_a_line_is_indexed_with_its_exact_seqs(simple: Indexed) -> None:
    """The golden list, for every line the program executed."""
    file_id = simple.file_id("simple.py")
    for lineno in {e.lineno for e in simple.events if e.kind is EventKind.LINE}:
        assert hits_of(simple.db, file_id, lineno) == _line_seqs(simple, lineno)


def test_next_and_previous_hit_are_mirror_images(simple: Indexed) -> None:
    """Continue-to-breakpoint and reverse-continue, at every boundary.

    Both strict, so standing *on* a hit and continuing moves to the next one rather than
    leaving you where you are -- the behaviour every debugger's `continue` has.
    """
    file_id = simple.file_id("simple.py")
    lineno = max(
        {e.lineno for e in simple.events if e.kind is EventKind.LINE},
        key=lambda ln: len(_line_seqs(simple, ln)),
    )
    hits = hits_of(simple.db, file_id, lineno)
    assert len(hits) > 1, "pick a line that runs more than once"
    for i, seq in enumerate(hits):
        expected_next = hits[i + 1] if i + 1 < len(hits) else None
        assert next_hit(simple.db, file_id, lineno, seq) == expected_next
        assert previous_hit(simple.db, file_id, lineno, seq) == (hits[i - 1] if i else None)


def test_a_line_that_never_ran_has_no_hits(simple: Indexed) -> None:
    """A breakpoint on a comment or a dead branch answers "never", not an error."""
    file_id = simple.file_id("simple.py")
    assert hits_of(simple.db, file_id, 99999) == []
    assert next_hit(simple.db, file_id, 99999, 0) is None
    assert previous_hit(simple.db, file_id, 99999, 10**9) is None


def test_the_heatmap_counts_match_the_event_stream(simple: Indexed) -> None:
    """A GROUP BY, not a materialised table -- so it must agree with the rows it groups."""
    file_id = simple.file_id("simple.py")
    expected: dict[int, int] = {}
    for event in simple.events:
        if event.kind is EventKind.LINE:
            expected[event.lineno] = expected.get(event.lineno, 0) + 1
    assert heatmap(simple.db, file_id) == expected


def test_the_breakpoint_query_uses_the_index(simple: Indexed) -> None:
    """A full scan here is a `continue` that gets slower the longer the recording."""
    plan = " ".join(
        str(row[-1])
        for row in simple.db.execute(
            "EXPLAIN QUERY PLAN SELECT seq FROM line_hits "
            "WHERE file_id=? AND lineno=? AND seq>? ORDER BY seq LIMIT 1",
            (0, 18, 0),
        )
    )
    assert "SCAN" not in plan.upper(), plan


def test_every_indexed_hit_resolves_to_a_real_source_file(tmp_path: Path) -> None:
    """`exec`'d code reports `<string>` and belongs to no file a user can open, so a
    breakpoint could never be set on it and its rows would only inflate the largest
    table in the index."""
    indexed = index_example(tmp_path, "simple")
    paths = {ident: path for path, ident in indexed.files.items()}
    for (file_id,) in indexed.db.execute("SELECT DISTINCT file_id FROM line_hits"):
        assert Path(paths[file_id]).suffix == ".py"
