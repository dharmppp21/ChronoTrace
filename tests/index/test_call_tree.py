"""The call tree, and the day's central claim: intervals encode time, not ancestry.

Two golden checks (a handwritten tree, liveness at every boundary) and one differential
check that exists because the tempting optimisation is wrong.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from chronotrace.index import children_of, descendants_of, live_at
from chronotrace.recorder.events import EventKind

from .conftest import Indexed, index_example


def _tree(indexed: Indexed) -> dict[str, list[str]]:
    """The call tree as qualnames, so the expectation is readable by a human."""
    by_id = {ident: q for q, ident in indexed.codes.items()}
    rows = indexed.db.execute(
        "SELECT frame_id, code_id, parent_frame_id FROM frames ORDER BY entry_seq"
    ).fetchall()
    name_of = {frame: by_id[code] for frame, code, _p in rows}
    out: dict[str, list[str]] = {}
    for frame, _code, parent in rows:
        if parent is not None:
            out.setdefault(name_of[parent], []).append(name_of[frame])
    return out


def test_the_tree_matches_what_a_human_wrote_down(simple: Indexed) -> None:
    """`examples/simple.py`: main calls quadruple twice, each calls double twice.

    The expectation is the call graph read off the source, not off the index -- if they
    disagree, the index is what gets questioned.
    """
    assert _tree(simple) == {
        "main": ["quadruple", "quadruple"],
        "quadruple": ["double", "double", "double", "double"],
    }


def test_live_at_is_correct_at_every_frame_boundary(simple: Indexed) -> None:
    """Half-open `[entry, exit)`, checked at the exact instants it could be off by one.

    At `entry_seq` the frame is live; at `exit_seq` it is already gone. A closed interval
    would report a dead frame as live for one `seq`, which is a stale row in the call-stack
    panel on every return.
    """
    rows = simple.db.execute("SELECT frame_id, entry_seq, exit_seq FROM frames").fetchall()
    assert rows, "the fixture must have frames"
    for frame_id, entry, exit_seq in rows:
        assert frame_id in {f for f, _c, _e in live_at(simple.db, entry)}, "live at entry"
        if exit_seq is not None:
            at_exit = {f for f, _c, _e in live_at(simple.db, exit_seq)}
            assert frame_id not in at_exit, f"frame {frame_id} still live at its exit_seq"
            assert frame_id in {f for f, _c, _e in live_at(simple.db, exit_seq - 1)}


def test_live_at_agrees_with_reconstruction(simple: Indexed) -> None:
    """The index and the reconstructor must name the same live frames at the same instant.

    Two independent derivations of the same fact -- one from `frames` intervals, one from
    keyframes and deltas -- so this catches either drifting from the other.
    """
    from chronotrace.reconstruct import KeyframeReconstructor
    from chronotrace.store import ChronoReader

    with ChronoReader.open(simple.recording) as reader:
        reconstructor = KeyframeReconstructor(reader)
        for seq in range(len(reader)):
            indexed_frames = {f for f, _c, _e in live_at(simple.db, seq)}
            state_frames = {f.frame_id for f in reconstructor.reconstruct(seq).frames}
            assert indexed_frames == state_frames, f"disagreement at seq {seq}"


def test_a_suspended_generator_is_live_but_not_executing(tmp_path: Path) -> None:
    """The distinction the UI depends on, and the reason day 6 abandoned the stack.

    A yielded generator holds real locals a user can inspect while sitting on no stack at
    all. `live_at` must include it; `ProgramState.current_frame_id` must not be it.
    """
    from chronotrace.reconstruct import KeyframeReconstructor
    from chronotrace.store import ChronoReader

    indexed = index_example(tmp_path, "generators", "pipeline")
    assert any(e.kind is EventKind.YIELD for e in indexed.events), "the fixture must suspend"

    divergences = 0
    with ChronoReader.open(indexed.recording) as reader:
        reconstructor = KeyframeReconstructor(reader)
        for seq in range(len(reader)):
            state = reconstructor.reconstruct(seq)
            suspended = {f.frame_id for f in state.frames if f.suspended}
            if not suspended:
                continue
            live = {f for f, _c, _e in live_at(indexed.db, seq)}
            assert suspended <= live, f"a suspended generator went missing from the index at {seq}"
            # At the YIELD instant itself the suspending frame *is* the executing one --
            # it ran the event that suspended it. The interesting instants are the ones
            # after, where the consumer runs while the generator sits on no stack.
            divergences += state.current_frame_id not in suspended
    assert divergences, "control never moved on while a generator was suspended"


# -- the day's finding -------------------------------------------------------------------


def _descendants_by_interval(db: sqlite3.Connection, frame_id: int) -> list[int]:
    """The tempting optimisation: a descendant is a frame whose interval nests inside F's.

    Kept **only** to demonstrate that it is wrong. One indexed range scan instead of a
    recursive walk, which is why it is tempting; see `call_tree.py` for why it does not
    hold once frames can suspend.
    """
    row = db.execute(
        "SELECT entry_seq, exit_seq FROM frames WHERE frame_id=?", (frame_id,)
    ).fetchone()
    if row is None:
        return []
    entry, exit_seq = row[0], row[1] if row[1] is not None else 1 << 62
    return sorted(
        int(f)
        for (f,) in db.execute(
            "SELECT frame_id FROM frames WHERE entry_seq > ? AND entry_seq < ?", (entry, exit_seq)
        )
    )


def test_descendants_match_the_recursive_cte_oracle(simple: Indexed) -> None:
    """On a plain call tree both agree -- which is why the trap is easy to fall into."""
    for (frame_id,) in simple.db.execute("SELECT frame_id FROM frames"):
        assert descendants_of(simple.db, frame_id) == _descendants_by_interval(
            simple.db, frame_id
        ), f"frame {frame_id}"


def test_intervals_stop_encoding_ancestry_once_frames_suspend(tmp_path: Path) -> None:
    """The measured counter-example, pinned so the optimisation is never "rediscovered".

    In `interleaved_generators`, two generators of the same function are alive at once:
    each **outlives its parent** (finalised after the caller returned) and **overlaps its
    sibling**. Both invariants a nested-set encoding needs are violated, so interval
    containment reports frames that are not descendants and misses ones that are.

    Generators broke the stack model on day 6 and they break the interval-as-tree model
    here, for the same reason: a frame's lifetime is not its position in a call tree.
    """
    indexed = index_example(tmp_path, "generators", "interleaved_generators")
    rows = indexed.db.execute("SELECT frame_id, entry_seq, exit_seq, parent_frame_id FROM frames")
    frames = [(int(f), int(e), x, p) for f, e, x, p in rows]
    assert len(frames) >= 3, "the fixture must create several live generators"

    disagreements = [
        frame_id
        for frame_id, _e, _x, _p in frames
        if descendants_of(indexed.db, frame_id) != _descendants_by_interval(indexed.db, frame_id)
    ]
    assert disagreements, (
        "interval containment agreed with the call tree here, which would mean the "
        "counter-example stopped holding -- re-derive it before trusting intervals:\n"
        f"{frames}"
    )


def test_children_are_returned_in_call_order(simple: Indexed) -> None:
    """The tree panel renders them top to bottom in the order they were called."""
    for (frame_id,) in simple.db.execute("SELECT frame_id FROM frames"):
        entries = [entry for _f, _c, entry in children_of(simple.db, frame_id)]
        assert entries == sorted(entries)


def test_a_frame_that_never_exits_is_recorded_as_still_open(tmp_path: Path) -> None:
    """A truncated recording ends mid-call. Those frames were live at the end, and the
    range predicate already handles `exit_seq IS NULL` -- verified rather than assumed."""
    indexed = index_example(tmp_path, "generators", "abandoned_generator")
    open_frames = indexed.db.execute(
        "SELECT frame_id, entry_seq FROM frames WHERE exit_seq IS NULL"
    ).fetchall()
    total = indexed.db.execute("SELECT count(*) FROM frames").fetchone()[0]
    assert total > 0
    for frame_id, entry in open_frames:
        assert frame_id in {f for f, _c, _e in live_at(indexed.db, entry)}
        last = len(indexed.events) - 1
        assert frame_id in {f for f, _c, _e in live_at(indexed.db, last)}, "still open at the end"


def test_the_live_at_query_uses_the_index(simple: Indexed) -> None:
    """It runs on every scrub, so a full scan here is a dragged playhead that stutters."""
    plan = " ".join(
        str(row[-1])
        for row in simple.db.execute(
            "EXPLAIN QUERY PLAN SELECT frame_id FROM frames "
            "WHERE entry_seq <= ? AND (exit_seq > ? OR exit_seq IS NULL) ORDER BY entry_seq",
            (10, 10),
        )
    )
    assert "SCAN" not in plan.upper(), plan
