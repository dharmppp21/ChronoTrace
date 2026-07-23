"""Two questions over the call tree: *"who called F?"* and *"what did this frame call?"*.

Problem this solves: the structural questions a debugger's call-stack and tree panels ask,
answered as jumpable instants. *"Who called `parse`?"* is every invocation of it, each a
place you can land at the moment of the call; *"what did frame 7 call?"* is its children, in
the order they ran.

Interface: `CallersOfQuery(function)` and `CallTreeQuery(frame_id)`.

It must never know: how a tree is drawn. It returns call instants (`entry_seq`s).

Built on the day-27 call tree
-----------------------------
`frames` carries `(code_id, parent_frame_id, entry_seq)`, indexed by code and by parent, so
both queries are indexed range scans in call order -- no walking. Note what each returns: a
*call instant*, the `entry_seq` where a frame began, which is the moment the call happened
and the place a user wants to jump to. Following ancestry uses `parent_frame_id`, never the
`entry_seq` interval -- day 27 proved those intervals encode time, not the tree, and a
suspended generator breaks interval containment.
"""

from __future__ import annotations

from dataclasses import dataclass

from chronotrace.query._resolve import Location, frame_location
from chronotrace.query.types import (
    PAGE_SIZE,
    Cursor,
    Hit,
    QueryContext,
    QueryResult,
    UnknownFunction,
    after_bound,
)


@dataclass(frozen=True, slots=True)
class CallersOfQuery:
    """Every invocation of `function`, in call order, showing where each was called from.

    Attributes:
        function: the qualified name (`"Parser.parse"`, `"main"`), as it appears in the
            source. Several code objects can share one qualname (a redefinition, a
            comprehension); all of their invocations are returned.
    """

    function: str

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = PAGE_SIZE
    ) -> QueryResult:
        """Return one page of invocations. Raises `UnknownFunction` if the name is a typo."""
        code_ids = [
            int(r[0])
            for r in ctx.db.execute(
                "SELECT code_id FROM codes WHERE qualname = ?", (self.function,)
            )
        ]
        if not code_ids:
            raise UnknownFunction(f"no function named {self.function!r} was recorded")
        after = after_bound(cursor)
        placeholders = ",".join("?" * len(code_ids))  # bound params, not user text
        sql = (
            f"SELECT frame_id, entry_seq, parent_frame_id FROM frames "  # noqa: S608
            f"WHERE code_id IN ({placeholders}) AND entry_seq > ? ORDER BY entry_seq LIMIT ?"
        )
        rows = ctx.db.execute(sql, (*code_ids, after, limit + 1)).fetchall()
        seen: dict[int, Location] = {}
        hits = [self._hit(ctx, int(fid), int(entry), parent, seen) for fid, entry, parent in rows]
        return QueryResult.page(hits, limit=limit, partial=ctx.partial)

    def _hit(
        self,
        ctx: QueryContext,
        frame_id: int,
        entry_seq: int,
        parent: int | None,
        seen: dict[int, Location],
    ) -> Hit:
        """One invocation as a jumpable call instant, noting the caller it came from."""
        function, file = frame_location(ctx.db, frame_id, seen)
        caller = frame_location(ctx.db, parent, seen)[0] if parent is not None else None
        return Hit(
            seq=entry_seq,
            file=file,
            function=function,
            note=f"called from {caller!r}"
            if caller is not None
            else "called from an unrecorded frame",
        )


@dataclass(frozen=True, slots=True)
class CallTreeQuery:
    """The direct children of `frame_id`, in call order -- one level of the call tree.

    Attributes:
        frame_id: the frame whose children to list. One level, because that is what a tree
            view expands; the whole subtree of a busy frame would be a wall of rows.
    """

    frame_id: int

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = PAGE_SIZE
    ) -> QueryResult:
        """Return one page of the frame's direct children, oldest call first."""
        after = after_bound(cursor)
        rows = ctx.db.execute(
            "SELECT frame_id, entry_seq FROM frames "
            "WHERE parent_frame_id = ? AND entry_seq > ? ORDER BY entry_seq LIMIT ?",
            (self.frame_id, after, limit + 1),
        ).fetchall()
        seen: dict[int, Location] = {}
        hits = [self._hit(ctx, int(fid), int(entry), seen) for fid, entry in rows]
        return QueryResult.page(hits, limit=limit, partial=ctx.partial)

    def _hit(
        self, ctx: QueryContext, frame_id: int, entry_seq: int, seen: dict[int, Location]
    ) -> Hit:
        function, file = frame_location(ctx.db, frame_id, seen)
        return Hit(seq=entry_seq, file=file, function=function, note="child call")
