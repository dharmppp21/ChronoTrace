"""Retroactive breakpoints: the breakpoint you wish you had set, on a run that already happened.

The pitch in one sentence -- *set a breakpoint after the program finished, and see every time
it would have hit, without re-running.* No `pdb` can do it, because there is nothing left to
run. Here it is a query: every `seq` where the line executed (the day-27 line index), and --
optionally -- only those where a condition held.

Interface: `RetroBreakpointQuery(file, lineno, condition=None)`.

It must never know: how an instant is jumped to, or what `eval` is (the evaluator lives in
`expr.py` and never runs user code).

Conditional breakpoints without reconstructing 10,000 times
-----------------------------------------------------------
`i > 100` on a line hit 10,000 times needs the program state at each hit -- and reconstructing
at all of them is far over budget. Three ideas, each a real query-planner technique:

* **Predicate pushdown.** The condition's value can only change when one of its variables
  changes. So a hit where none of the condition's names were written since the last hit has
  the *same* answer as the last hit -- carried forward, not recomputed. The pushdown is
  **conservative**: a write anywhere to a named variable forces a re-evaluation, so it can
  only ever skip a hit that provably could not have flipped, never one that could.
* **Incremental reconstruction.** Candidates come in `seq` order, so evaluation walks forward
  through the day-20 locality cache instead of restarting from a keyframe each time -- a
  sequential scan with a cursor, not N random seeks.
* **Lazy under a cursor.** The first page returns after finding its `limit` matches; it does
  not evaluate 10,000 conditions to show 20 rows.

Honesty about what could not be seen
------------------------------------
A hit where the condition is *true* is a match. A hit where it is *false* is skipped. A hit
where the condition is **unknown** -- it needed a value the recording only summarised (a
truncated string, a redacted secret, a name out of scope) -- is returned, flagged, because
silently dropping it would be claiming `false` for something we could not evaluate. That is
the rule `expr.py` exists to enforce, surfaced here as a distinct kind of result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from chronotrace.query._resolve import name_id, resolve_file
from chronotrace.query.expr import ConditionError, compile_condition
from chronotrace.query.types import (
    PAGE_SIZE,
    Cursor,
    Hit,
    QueryContext,
    QueryError,
    QueryResult,
    UnknownName,
    after_bound,
)
from chronotrace.recorder.events import EventKind

if TYPE_CHECKING:
    import sqlite3

    from chronotrace.query.expr import Condition
    from chronotrace.recorder.events import Event
    from chronotrace.store import ChronoReader

_BATCH = 2048
"""Line hits fetched per round trip while scanning for matches -- bounded so a condition that
matches nothing does not pull a million-hit line into memory to find zero rows."""


@dataclass(frozen=True, slots=True)
class RetroBreakpointQuery:
    """Every instant `file:lineno` executed, optionally filtered by a condition.

    Attributes:
        file: a path or bare filename, resolved against the recording (see `resolve_file`).
        lineno: the 1-based line the breakpoint sits on.
        condition: a restricted expression (`expr.py`) evaluated in the frame at each hit;
            None means an unconditional breakpoint -- every hit.
    """

    file: str
    lineno: int
    condition: str | None = None

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = PAGE_SIZE
    ) -> QueryResult:
        """One page of hits. Raises `UnknownFile`, or `QueryError` for a bad condition."""
        file_id, path = resolve_file(ctx.db, self.file)
        after = after_bound(cursor)
        if self.condition is None:
            rows = ctx.db.execute(
                "SELECT seq FROM line_hits WHERE file_id=? AND lineno=? AND seq>? "
                "ORDER BY seq LIMIT ?",
                (file_id, self.lineno, after, limit + 1),
            ).fetchall()
            hits = [Hit(seq=int(s), file=path, lineno=self.lineno) for (s,) in rows]
            return QueryResult.page(hits, limit=limit, partial=ctx.partial)
        return self._conditional(ctx, file_id, path, after, limit)

    def _conditional(
        self, ctx: QueryContext, file_id: int, path: str, after: int, limit: int
    ) -> QueryResult:
        """Scan hits lazily, evaluating the condition with pushdown, until `limit` matches."""
        try:
            cond = compile_condition(self.condition or "")
        except ConditionError as exc:
            raise QueryError(str(exc)) from exc
        ids = {name: nid for name in cond.names if (nid := _maybe_id(ctx.db, name)) is not None}
        var_ids = list(ids.values())
        hits: list[Hit] = []
        prev_seq, outcome, evaluated = after, None, False  # outcome carried forward (pushdown)
        for seq in self._hits_after(ctx.db, file_id, after):
            if len(hits) > limit:
                break
            if not evaluated or _changed(ctx.db, var_ids, prev_seq, seq):
                outcome = self._evaluate(ctx, cond, ids, seq)
                evaluated = True
            prev_seq = seq
            hit = self._hit(path, seq, outcome)
            if hit is not None:
                hits.append(hit)
        return QueryResult.page(hits, limit=limit, partial=ctx.partial)

    def _hit(self, path: str, seq: int, outcome: bool | None) -> Hit | None:
        """A match, a flagged unknown, or None (a definite miss -- skipped silently)."""
        if outcome is True:
            return Hit(seq=seq, file=path, lineno=self.lineno, note=f"{self.condition} -> true")
        if outcome is None:
            return Hit(
                seq=seq,
                file=path,
                lineno=self.lineno,
                note=f"{self.condition} -> UNKNOWN (a value here was summarised, redacted, "
                "or out of scope)",
            )
        return None

    def _evaluate(
        self, ctx: QueryContext, cond: Condition, ids: dict[str, int], seq: int
    ) -> bool | None:
        """Evaluate the condition in the arrival namespace of the line hit at `seq`.

        Not `reconstruct(seq)`: the recorder emits a line's local captures *after* its LINE
        event and before the line runs, so the LINE instant is missing the very variables the
        line reads -- a loop variable, a parameter on the function's first line. Advancing past
        those captures (`_capture_end`) gives the namespace on *arrival* at the line, which is
        exactly what a `pdb` conditional breakpoint evaluates against. (Day 15/19 semantics:
        state *after* an instant; the instant chosen is the end of the line's own captures.)
        """
        from contextlib import suppress

        from chronotrace.reconstruct import MissingValue

        state = ctx.reconstructor.reconstruct(_capture_end(ctx.reader, seq))
        frame = state.frame(state.current_frame_id)
        if frame is None:
            return None  # no live frame ran this instant: the condition cannot be evaluated
        bindings: dict[str, Any] = {}
        for name, nid in ids.items():
            if nid in frame.bindings:
                # unresolvable -> left absent -> UNKNOWN in eval, never a fabricated value
                with suppress(MissingValue):
                    bindings[name] = ctx.resolver.resolve(frame.bindings[nid])
        return cond.evaluate(bindings)

    def _hits_after(self, db: sqlite3.Connection, file_id: int, after: int) -> Any:
        """Yield the line's hit seqs after `after`, in order, a batch at a time (lazy)."""
        cursor = after
        while True:
            rows = db.execute(
                "SELECT seq FROM line_hits WHERE file_id=? AND lineno=? AND seq>? "
                "ORDER BY seq LIMIT ?",
                (file_id, self.lineno, cursor, _BATCH),
            ).fetchall()
            for (seq,) in rows:
                yield int(seq)
            if len(rows) < _BATCH:
                return
            cursor = int(rows[-1][0])


def _capture_end(reader: ChronoReader, seq: int) -> int:
    """The instant a line's own local captures finish -- the namespace on arrival at the line.

    A LINE event is followed immediately by the `VAR_WRITE`s that capture the line's locals,
    and only then does the line execute. So the state *after* those writes is the frame's
    namespace as the line is entered -- a parameter, a loop variable -- which is what a
    conditional breakpoint must see. The scan is bounded by the count of locals captured on
    one line (a handful), not by the recording.
    """
    end = len(reader)
    at = seq + 1
    while at < end and cast("Event", reader[at]).kind is EventKind.VAR_WRITE:
        at += 1
    return at - 1


def _maybe_id(db: sqlite3.Connection, name: str) -> int | None:
    """The recording's id for `name`, or None if it was never recorded (never written)."""
    try:
        return name_id(db, name)
    except UnknownName:
        return None  # an unrecorded name is simply a variable that never changed


def _changed(db: sqlite3.Connection, name_ids: list[int], lo: int, hi: int) -> bool:
    """Was any of `name_ids` written in `(lo, hi]`? The conservative pushdown test.

    Not frame-scoped on purpose: a write to a same-named variable in another frame forces a
    re-evaluation we do not strictly need, which is wasteful but never wrong. Missing a real
    change would be wrong, and a per-frame binding always emits a write when it first appears
    (day-8 dedup is per frame), so no real change is missed.
    """
    if not name_ids:
        return False  # a condition with no recorded variables is constant across hits
    placeholders = ",".join("?" * len(name_ids))  # bound params, not user text
    sql = (
        f"SELECT 1 FROM var_writes WHERE name_id IN ({placeholders}) "  # noqa: S608
        "AND seq>? AND seq<=? LIMIT 1"
    )
    return db.execute(sql, (*name_ids, lo, hi)).fetchone() is not None
