"""*"Who last wrote to `x` before here?"* -- the single instant, in O(log n).

Problem this solves: it is the primitive every debugging session reaches for, and the one
the demo turns on. `VarWritesQuery` returns *every* write; this returns the *one* that
produced the value you are looking at, without paging through the rest. Given a variable
written a million times, it answers as fast as one written twice -- a descending seek on the
`(name_id, seq)` clustered key that stops at the first row.

Interface: `LastWriteBeforeQuery(name, seq, frame_id=None)`, run via `execute`.

It must never know: how a result is rendered. It returns the writing instant, as a `seq`.

Strictly before, not at-or-before
----------------------------------
The caller stands at `seq` asking who put this value here; the write *at* `seq` is the one
they are already looking at, so it is excluded. This is the same boundary
`index.last_write_before` draws, and the two must agree -- the query is a thin, display-
adding shell over that index helper.
"""

from __future__ import annotations

from dataclasses import dataclass

from chronotrace.index.var_writes import last_write_before
from chronotrace.query._resolve import Location, frame_location, name_id, value_preview
from chronotrace.query.types import Cursor, Hit, QueryContext, QueryResult


@dataclass(frozen=True, slots=True)
class LastWriteBeforeQuery:
    """The most recent write to `name` strictly before `seq`; at most one hit.

    Attributes:
        name: the variable, as typed. Resolved to the recording's `name_id`.
        seq: the instant to look back from. The write returned is strictly before it.
        frame_id: restrict to one invocation -- necessary under recursion, where several
            live frames hold a variable of the same name.
    """

    name: str
    seq: int
    frame_id: int | None = None

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = 1
    ) -> QueryResult:
        """Return the single last write, or an empty result. Raises `UnknownName` on a typo.

        `cursor`/`limit` are accepted to satisfy the `Query` protocol but do nothing here:
        the answer is one instant, never a page. A `cursor` (there is no next page) yields
        an empty result, which is the honest response to "page past a single answer".
        """
        if cursor is not None:
            return QueryResult.empty(ctx.partial)
        resolved = name_id(ctx.db, self.name)
        write = last_write_before(ctx.db, resolved, self.seq, frame_id=self.frame_id)
        if write is None:
            return QueryResult.empty(ctx.partial)
        write_seq, frame_id, value_ref = write
        seen: dict[int, Location] = {}
        function, file = frame_location(ctx.db, frame_id, seen)
        hit = Hit(
            seq=write_seq,
            function=function,
            file=file,
            value_preview=value_preview(ctx, value_ref),
            note=f"the last write to {self.name!r} before seq {self.seq}",
        )
        return QueryResult((hit,), None, ctx.partial)
