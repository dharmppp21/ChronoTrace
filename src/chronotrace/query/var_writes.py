"""The flagship query: *"who wrote `total`, and to what?"* -- as jumpable instants.

Problem this solves: the question a debugger exists to answer is "where did this value come
from?", and the honest answer is not a number, it is an *instant* -- the moment the write
happened, which you then jump to and inspect. This query returns those instants, each
carrying enough context (which function, what value) to pick the one you meant before you
land on it.

Interface: `VarWritesQuery(name, frame_id=None, before_seq=None)`, run via `execute`.

It must never know: how a result is rendered or what "jump" means. It returns `seq`s.

Built on the day-26 index
-------------------------
Every fact here comes from `var_writes`, clustered on `(name_id, seq)`, so a page is an
indexed range scan already in `seq` order -- no sort. The value preview and the function
name are enrichment, resolved only for the page actually returned, so the cost of decoding
values is bounded by `limit` and never by how many times the variable was written.

`frame_id` is why recursion does not lie
----------------------------------------
`total` in one call and `total` in another are different variables sharing a name. Without
`frame_id` the query would merge them and answer "who wrote total" with some other call's
write. Passing `frame_id` scopes the question to one invocation; omitting it asks across
all of them, newest instants last. Day 6's per-frame identity is what makes the distinction
real rather than a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chronotrace.query._resolve import Location, frame_location, name_id, value_preview
from chronotrace.query.types import PAGE_SIZE, Cursor, Hit, QueryContext, QueryResult, after_bound

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True, slots=True)
class VarWritesQuery:
    """Every write to `name`, oldest instant first; optionally one frame, before one instant.

    Attributes:
        name: the variable, as the user typed it. Resolved to the recording's `name_id`.
        frame_id: restrict to a single invocation (necessary under recursion).
        before_seq: only writes strictly before this instant -- "who last set this, before
            it went wrong?" is `before_seq` plus reading the last page.
    """

    name: str
    frame_id: int | None = None
    before_seq: int | None = None

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = PAGE_SIZE
    ) -> QueryResult:
        """Return one page of writes. Raises `UnknownName` if the variable never existed."""
        resolved = name_id(ctx.db, self.name)
        rows = self._page(ctx.db, resolved, cursor, limit)
        seen: dict[int, Location] = {}
        hits = [
            self._hit(ctx, int(seq), int(frame_id), int(value_ref), seen)
            for seq, frame_id, value_ref in rows
        ]
        return QueryResult.page(hits, limit=limit, partial=ctx.partial)

    def _hit(
        self,
        ctx: QueryContext,
        seq: int,
        frame_id: int,
        value_ref: int,
        seen: dict[int, Location],
    ) -> Hit:
        """One write turned into a jumpable instant: where it happened, what it wrote."""
        function, file = frame_location(ctx.db, frame_id, seen)
        return Hit(
            seq=seq, function=function, file=file, value_preview=value_preview(ctx, value_ref)
        )

    def _page(
        self, db: sqlite3.Connection, name_id: int, cursor: Cursor | None, limit: int
    ) -> list[tuple[int, int, int]]:
        """One `limit + 1` page of `(seq, frame_id, value_ref)`, in `seq` order.

        The extra row is the pagination sentinel (`QueryResult.page`). The `WHERE` clauses
        are assembled from literal fragments and bound parameters -- no user text enters the
        SQL -- so the composition is safe despite the f-string.
        """
        after = after_bound(cursor)
        conds = ["name_id = ?", "seq > ?"]
        params: list[int] = [name_id, after]
        if self.frame_id is not None:
            conds.append("frame_id = ?")
            params.append(self.frame_id)
        if self.before_seq is not None:
            conds.append("seq < ?")
            params.append(self.before_seq)
        params.append(limit + 1)
        where = " AND ".join(conds)  # literal fragments only; every value is a bound param
        sql = (
            f"SELECT seq, frame_id, value_ref FROM var_writes "  # noqa: S608
            f"WHERE {where} ORDER BY seq LIMIT ?"
        )
        return list(db.execute(sql, params))
