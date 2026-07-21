"""The call tree: who called whom, and which frames were alive at any instant.

Problem this solves: the UI needs two structural questions answered instantly — *which
frames were live at `seq`?* (the call-stack panel, on every scrub) and *what did frame F
call?* (expanding a node in the tree).

Interface: `CallTreeIndexer`, plus `live_at`, `children_of`, `descendants_of`.

It must never know: how a tree is drawn, or what the user clicked.

The interval convention, defined once
--------------------------------------
A frame's life is the **half-open** interval `[entry_seq, exit_seq)`. Half-open because
`exit_seq` is the instant the frame *left*, and at that instant the frame is already gone
from the reconstructed state — a closed interval would report a dead frame as live for one
`seq` and put an off-by-one into every panel that shows a stack. A frame that never exits
(the recording was truncated, or a generator was never finalised) has `exit_seq IS NULL`,
which reads as "still open at the end of the recording".

**live** vs **executing** — the UI needs both, and they are different
---------------------------------------------------------------------
* **live**: the frame exists at `seq`. Includes a suspended generator, which holds real
  locals a user can inspect while sitting on no stack at all. This is `live_at`.
* **executing**: the single frame that ran the event at `seq`. That is
  `ProgramState.current_frame_id`, and reconstruction already answers it.

The call-stack panel shows *live* frames; the highlighted row is the *executing* one. Day
6 chose a registry over a stack precisely because these two stopped coinciding, and the
distinction survives here.

`entry_seq`/`exit_seq` are an interval encoding — of **time**, not of the tree
-------------------------------------------------------------------------------
It is tempting to notice that these intervals look like a nested-set encoding and get
subtree queries for free: a descendant would be any frame whose interval nests inside F's,
which is one indexed range scan instead of a recursive walk.

**That is wrong here, and generators are why** — the same feature that killed the stack
model on day 6. Measured on `examples/generators.py::interleaved_generators`:

    frame 1  [ 0, 19)  parent=None     <- the caller
    frame 2  [ 3, 25)  parent=1        <- a generator, OUTLIVES its parent
    frame 3  [ 7, 22)  parent=1        <- and OVERLAPS its sibling

Two invariants a nested-set encoding depends on are both violated: a child's interval is
not contained in its parent's (the generators are finalised after the caller returned),
and siblings overlap. Worse in the other direction: while a generator is suspended, every
unrelated frame that runs has an interval nesting *inside* the generator's, so nesting
would report strangers as descendants.

So intervals answer **liveness**, which is genuinely a time question, and the
`parent_frame_id` walk answers **ancestry**, which is a structure question. Using one for
the other is the mistake this docstring exists to prevent;
`test_call_tree.py::test_descendants_match_the_recursive_cte_oracle` pins it, and an
interval-based implementation is kept there purely to demonstrate that it disagrees.
"""

from __future__ import annotations

import sqlite3

from chronotrace.index.db import Batcher
from chronotrace.recorder.events import Event, EventKind

INSERT = (
    "INSERT OR REPLACE INTO frames"
    "(frame_id, code_id, parent_frame_id, entry_seq, exit_seq, exit_kind) VALUES (?,?,?,?,?,?)"
)

_EXIT = (EventKind.RETURN, EventKind.UNWIND)
"""How a frame can end. Both, so a frame that blew up is closed like any other."""


class CallTreeIndexer:
    """Builds `frames` from CALL/RETURN/UNWIND, tracking parentage as it goes.

    Parentage comes from the frame that was *executing* when the call happened, which the
    indexer knows because it sees the events in order — the same information reconstruction
    cannot recover from a keyframe alone, which is why ADR-0006 named this index the
    authority for `parent_frame_id`.

    Rows are held until `finalise` because a frame's `exit_seq` is not known when it
    enters. Memory is one small tuple per *live* frame, bounded by the program's stack
    depth plus its suspended generators -- not by the recording's length.
    """

    __slots__ = ("_batch", "_open", "_stack")

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._batch = Batcher(connection, INSERT)
        # frame_id -> (code_id, parent_frame_id, entry_seq). Held open until the frame
        # exits, because `exit_seq` is not known when it enters.
        self._open: dict[int, tuple[int, int | None, int]] = {}
        self._stack: list[int] = []

    def consume(self, event: Event) -> None:
        """Fold one event into the tree under construction."""
        kind, fid = event.kind, event.frame_id
        if kind is EventKind.CALL:
            parent = self._stack[-1] if self._stack else None
            self._open[fid] = (event.code_id, parent, event.seq)
            self._stack.append(fid)
        elif kind in _EXIT:
            self._close(fid, event)
        elif kind is EventKind.YIELD:
            # Suspension leaves the stack without ending the frame -- the day-6 model.
            # The frame stays open, so it stays live, and the *next* call is not its child.
            self._pop(fid)
        elif kind is EventKind.RESUME:
            self._stack.append(fid)

    def _close(self, fid: int, event: Event) -> None:
        entry = self._open.pop(fid, None)
        if entry is None:
            return  # a frame whose CALL predates the recording; nothing to close
        code_id, parent, entry_seq = entry
        self._batch.add((fid, code_id, parent, entry_seq, event.seq, int(event.kind)))
        self._pop(fid)

    def _pop(self, fid: int) -> None:
        """Remove `fid` from the execution stack, wherever it sits.

        Not always the top: an exception can unwind several frames, and a generator
        finalised by the collector exits from outside any call. Searching is safe because
        the stack is a program's call depth, not its event count.
        """
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i] == fid:
                del self._stack[i]
                return

    def finalise(self) -> None:
        """Write the frames that never exited, with `exit_seq` NULL.

        A truncated recording ends mid-call, and an abandoned generator may never be
        finalised at all. Those frames are real and were live at the end, so they are
        recorded as still open rather than dropped -- dropping them would make the
        call-stack panel go empty exactly where the program died.
        """
        for fid, (code_id, parent, entry_seq) in self._open.items():
            self._batch.add((fid, code_id, parent, entry_seq, None, None))
        self._open.clear()
        self._batch.flush()


def live_at(connection: sqlite3.Connection, seq: int) -> list[tuple[int, int, int]]:
    """Every frame live at `seq`, as `(frame_id, code_id, entry_seq)`, outermost first.

    The half-open interval predicate, and it handles a never-exiting frame for free:
    `exit_seq IS NULL` means still open.

    Complexity: O(log n + live) via `ix_frames_entry`. This runs on every scrub, so it is
    a range scan rather than a walk.
    """
    return [
        (int(f), int(c), int(e))
        for f, c, e in connection.execute(
            "SELECT frame_id, code_id, entry_seq FROM frames "
            "WHERE entry_seq <= ? AND (exit_seq > ? OR exit_seq IS NULL) ORDER BY entry_seq",
            (seq, seq),
        )
    ]


def children_of(connection: sqlite3.Connection, frame_id: int) -> list[tuple[int, int, int]]:
    """The direct children of `frame_id`, in call order. One level of the tree.

    Complexity: O(log n + children) via `ix_frames_parent`. The UI expands one level at a
    time, so this is the query it actually makes -- `descendants_of` is for search.
    """
    return [
        (int(f), int(c), int(e))
        for f, c, e in connection.execute(
            "SELECT frame_id, code_id, entry_seq FROM frames "
            "WHERE parent_frame_id = ? ORDER BY entry_seq",
            (frame_id,),
        )
    ]


def descendants_of(connection: sqlite3.Connection, frame_id: int) -> list[int]:
    """Every frame beneath `frame_id`, at any depth, in call order.

    A recursive CTE over `parent_frame_id`, **not** an interval containment test. See the
    module docstring: seq intervals encode time, and under generators a frame's interval
    contains unrelated strangers and fails to contain its own children.

    Complexity: O(subtree x log n) -- one indexed lookup per level of the walk. Bounded by
    the subtree's size rather than the recording's, which is what makes it acceptable.
    """
    rows = connection.execute(
        "WITH RECURSIVE subtree(frame_id) AS ("
        "  SELECT frame_id FROM frames WHERE parent_frame_id = ?"
        "  UNION ALL"
        "  SELECT f.frame_id FROM frames f JOIN subtree s ON f.parent_frame_id = s.frame_id"
        ") SELECT frame_id FROM subtree",
        (frame_id,),
    )
    return sorted(int(row[0]) for row in rows)
